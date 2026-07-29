"""SQLite 行与领域模型之间的纯转换。"""

from __future__ import annotations

import mimetypes
from datetime import datetime

import aiosqlite

from ...domain.models import (
    ConversationDirection,
    ConversationMessage,
    ConversationMessageType,
    ConversationQuote,
    ConversationStatus,
    FilePayload,
    ImagePayload,
    MessageOrigin,
    OutboundMessage,
    OutboundMessageType,
    OutboundPayload,
    OutboundStatus,
    OwnerBinding,
    TextPayload,
    TypingCancelPayload,
    TypingStartPayload,
)

type OutboundChatFields = tuple[
    ConversationMessageType,
    str,
    str | None,
    str | None,
    bytes | None,
]


def owner_from_row(row: aiosqlite.Row) -> OwnerBinding:
    return OwnerBinding(
        user_id=str(row["user_id"]),
        context_token=str(row["context_token"]),
        credential_fingerprint=str(row["credential_fingerprint"]),
        updated_at=datetime.fromisoformat(str(row["updated_at"])),
    )


def outbound_from_row(row: aiosqlite.Row) -> OutboundMessage:
    sent_at = str(row["sent_at"]) if row["sent_at"] else None
    return OutboundMessage(
        id=str(row["id"]),
        user_id=str(row["user_id"]),
        message_type=OutboundMessageType(str(row["message_type"])),
        status=OutboundStatus(str(row["status"])),
        last_error_code=(
            str(row["last_error_code"]) if row["last_error_code"] is not None else None
        ),
        created_at=datetime.fromisoformat(str(row["created_at"])),
        updated_at=datetime.fromisoformat(str(row["updated_at"])),
        sent_at=datetime.fromisoformat(sent_at) if sent_at else None,
    )


def conversation_from_row(row: aiosqlite.Row) -> ConversationMessage:
    """恢复聊天记录及其一层引用快照。"""

    return ConversationMessage(
        id=str(row["id"]),
        direction=ConversationDirection(str(row["direction"])),
        message_type=ConversationMessageType(str(row["message_type"])),
        text=str(row["text"]),
        file_name=str(row["file_name"]) if row["file_name"] is not None else None,
        mime_type=str(row["mime_type"]) if row["mime_type"] is not None else None,
        has_media=row["media"] is not None,
        status=ConversationStatus(str(row["status"])),
        origin=MessageOrigin(str(row["origin"])),
        error_code=str(row["error_code"]) if row["error_code"] is not None else None,
        occurred_at=datetime.fromisoformat(str(row["occurred_at"])),
        updated_at=datetime.fromisoformat(str(row["updated_at"])),
        quote=_quote_from_row(row),
    )


def outbound_chat_fields(payload: OutboundPayload) -> OutboundChatFields | None:
    """提取管理页面需要持久化的出站内容；输入状态不产生聊天气泡。"""

    if isinstance(payload, TextPayload):
        return (ConversationMessageType.TEXT, payload.text, None, None, None)
    if isinstance(payload, ImagePayload):
        return (
            ConversationMessageType.IMAGE,
            payload.caption or "",
            None,
            _detect_image_mime(payload.data),
            payload.data,
        )
    if isinstance(payload, FilePayload):
        mime_type = mimetypes.guess_type(payload.file_name)[0] or "application/octet-stream"
        return (
            ConversationMessageType.FILE,
            payload.caption or "",
            payload.file_name,
            mime_type,
            payload.data,
        )
    if isinstance(payload, (TypingStartPayload, TypingCancelPayload)):
        return None
    raise TypeError(f"Unsupported outbound payload: {type(payload).__name__}")


def _quote_from_row(row: aiosqlite.Row) -> ConversationQuote | None:
    if row["quote_id"] is not None:
        return ConversationQuote(
            id=str(row["quote_id"]),
            direction=ConversationDirection(str(row["quote_direction"])),
            message_type=ConversationMessageType(str(row["quote_message_type"])),
            text=str(row["quote_text"]),
            file_name=(str(row["quote_file_name"]) if row["quote_file_name"] is not None else None),
            title=None,
        )
    if row["native_quote_text"] is None and row["native_quote_title"] is None:
        return None

    native_type = (
        str(row["native_quote_type"])
        if row["native_quote_type"] is not None
        else ConversationMessageType.TEXT.value
    )
    return ConversationQuote(
        id=None,
        direction=None,
        message_type=ConversationMessageType(native_type),
        text=str(row["native_quote_text"]) if row["native_quote_text"] is not None else "",
        file_name=None,
        title=(str(row["native_quote_title"]) if row["native_quote_title"] is not None else None),
    )


def _detect_image_mime(data: bytes) -> str:
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
    # SVG 可能携带脚本；只把已识别的位图格式作为可内嵌图片返回。
    return "application/octet-stream"
