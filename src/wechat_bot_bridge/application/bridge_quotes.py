"""管理页面桥接引用的快照与降级文本格式。"""

from __future__ import annotations

from typing import Final

from ..domain.models import (
    ConversationDirection,
    ConversationMessage,
    ConversationMessageType,
    ConversationQuote,
    FilePayload,
    ImagePayload,
    OutboundPayload,
    TextPayload,
)

_EXCERPT_MAX_CHARS: Final[int] = 160
_MEDIA_LABELS: Final[dict[ConversationMessageType, str]] = {
    ConversationMessageType.TEXT: "",
    ConversationMessageType.IMAGE: "[图片] ",
    ConversationMessageType.VOICE: "[语音] ",
    ConversationMessageType.FILE: "[文件] ",
    ConversationMessageType.VIDEO: "[视频] ",
}
_MEDIA_PLACEHOLDERS: Final[dict[ConversationMessageType, frozenset[str]]] = {
    ConversationMessageType.TEXT: frozenset(),
    ConversationMessageType.IMAGE: frozenset({"[image]", "[图片]"}),
    ConversationMessageType.VOICE: frozenset({"[voice]", "[语音]"}),
    ConversationMessageType.FILE: frozenset({"[file]", "[文件]"}),
    ConversationMessageType.VIDEO: frozenset({"[video]", "[视频]"}),
}


def quote_from_message(message: ConversationMessage) -> ConversationQuote:
    """提取一层稳定快照，避免引用目标后续变化影响已发送消息。"""

    return ConversationQuote(
        id=message.id,
        direction=message.direction,
        message_type=message.message_type,
        text=message.text,
        file_name=message.file_name,
        title=None,
    )


def with_bridge_quote(
    payload: OutboundPayload,
    quote: ConversationQuote | None,
) -> OutboundPayload:
    """把结构化引用转换成微信可见、且不会冒充原生引用的普通文本。"""

    if quote is None:
        return payload

    prefix = format_quote_prefix(quote)
    if isinstance(payload, TextPayload):
        return TextPayload(text=f"{prefix}{payload.text}")
    if isinstance(payload, ImagePayload):
        caption = f"{prefix}{payload.caption}" if payload.caption else prefix.rstrip()
        return ImagePayload(data=payload.data, caption=caption)
    if isinstance(payload, FilePayload):
        caption = f"{prefix}{payload.caption}" if payload.caption else prefix.rstrip()
        return FilePayload(
            data=payload.data,
            file_name=payload.file_name,
            caption=caption,
        )
    raise ValueError("Typing status cannot quote a conversation message")


def format_quote_prefix(quote: ConversationQuote) -> str:
    """生成有方向、有媒体类型且长度受控的引用摘要。"""

    type_label = _MEDIA_LABELS[quote.message_type]
    content = " ".join(quote.text.split())
    if _is_media_placeholder(content, quote.message_type):
        content = ""
    if not content and quote.file_name:
        content = quote.file_name
    if not content:
        content = type_label.strip() or "[消息]"
        type_label = ""

    excerpt = f"{type_label}{content}"
    if len(excerpt) > _EXCERPT_MAX_CHARS:
        excerpt = f"{excerpt[: _EXCERPT_MAX_CHARS - 1]}…"
    speaker = "对方" if quote.direction is ConversationDirection.INBOUND else "我"
    return f"【桥接引用·{speaker}】{excerpt}\n"


def _is_media_placeholder(
    text: str,
    message_type: ConversationMessageType,
) -> bool:
    normalized_placeholders = {value.casefold() for value in _MEDIA_PLACEHOLDERS[message_type]}
    return text.casefold() in normalized_placeholders
