"""iLink 消息构造、长文本切片和入站解析。"""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

from .errors import ILinkProtocolError
from .models import (
    CdnMedia,
    ContentType,
    IncomingMessage,
    MediaKind,
    MediaReference,
    QuotedMessage,
    UpdateBatch,
    WireItemType,
    WireMessageState,
    WireMessageType,
)

_MEDIA_KIND_BY_ITEM_TYPE = {
    int(WireItemType.IMAGE): MediaKind.IMAGE,
    int(WireItemType.VOICE): MediaKind.VOICE,
    int(WireItemType.FILE): MediaKind.FILE,
    int(WireItemType.VIDEO): MediaKind.VIDEO,
}


def chunk_text(text: str, limit: int = 4000) -> tuple[str, ...]:
    """按自然边界切分超长文本，确保每段不超过 iLink 的安全长度。"""

    if not text:
        raise ValueError("Message text cannot be empty")
    if limit < 1:
        raise ValueError("limit must be positive")
    remaining = text
    chunks: list[str] = []
    while len(remaining) > limit:
        window = remaining[:limit]
        cut = -1
        for separator in ("\n\n", "\n", " "):
            index = window.rfind(separator)
            if index > limit * 3 // 10:
                cut = index + len(separator)
                break
        if cut < 1:
            cut = limit
        chunks.append(remaining[:cut])
        remaining = remaining[cut:]
    if remaining:
        chunks.append(remaining)
    return tuple(chunks)


def build_message(
    *,
    user_id: str,
    context_token: str,
    item: dict[str, Any],
) -> dict[str, Any]:
    """构造腾讯 `SendMessageReq.msg` 对象。"""

    return {
        "from_user_id": "",
        "to_user_id": user_id,
        "client_id": f"wechat-bot-bridge-{uuid4()}",
        "message_type": int(WireMessageType.BOT),
        "message_state": int(WireMessageState.FINISH),
        "context_token": context_token,
        "item_list": [item],
    }


def build_text_item(text: str) -> dict[str, Any]:
    return {
        "type": int(WireItemType.TEXT),
        "text_item": {"text": text},
    }


def build_image_item(
    *,
    encrypted_query_param: str,
    aes_key: str,
    ciphertext_size: int,
) -> dict[str, Any]:
    return {
        "type": int(WireItemType.IMAGE),
        "image_item": {
            "media": {
                "encrypt_query_param": encrypted_query_param,
                "aes_key": aes_key,
                "encrypt_type": 1,
            },
            "mid_size": ciphertext_size,
        },
    }


def build_file_item(
    *,
    encrypted_query_param: str,
    aes_key: str,
    plaintext_size: int,
    file_name: str,
) -> dict[str, Any]:
    return {
        "type": int(WireItemType.FILE),
        "file_item": {
            "media": {
                "encrypt_query_param": encrypted_query_param,
                "aes_key": aes_key,
                "encrypt_type": 1,
            },
            "file_name": file_name,
            "len": str(plaintext_size),
        },
    }


def parse_update_batch(payload: dict[str, Any], *, current_cursor: str) -> UpdateBatch:
    """把 `getupdates` 响应转换为稳定内部模型。"""

    raw_cursor = payload.get("get_updates_buf", current_cursor)
    if not isinstance(raw_cursor, str):
        raise ILinkProtocolError("getupdates returned an invalid cursor")
    raw_messages = payload.get("msgs", [])
    if raw_messages is None:
        raw_messages = []
    if not isinstance(raw_messages, list):
        raise ILinkProtocolError("getupdates returned an invalid message list")

    messages: list[IncomingMessage] = []
    for raw in raw_messages:
        if not isinstance(raw, dict):
            continue
        parsed = parse_incoming_message(raw)
        if parsed is not None:
            messages.append(parsed)

    suggested_timeout = payload.get("longpolling_timeout_ms")
    if (
        not isinstance(suggested_timeout, int)
        or isinstance(suggested_timeout, bool)
        or suggested_timeout <= 0
    ):
        suggested_timeout = None
    return UpdateBatch(
        cursor=raw_cursor,
        messages=tuple(messages),
        suggested_timeout_ms=suggested_timeout,
    )


def parse_incoming_message(raw: dict[str, Any]) -> IncomingMessage | None:
    """解析一条用户消息；机器人回显和缺少发送者的异常项会被忽略。"""

    if raw.get("message_type") != int(WireMessageType.USER):
        return None
    user_id = raw.get("from_user_id")
    if not isinstance(user_id, str) or not user_id:
        return None
    context_token = raw.get("context_token")
    if not isinstance(context_token, str):
        context_token = ""

    raw_items = raw.get("item_list", [])
    items = (
        [item for item in raw_items if isinstance(item, dict)]
        if isinstance(raw_items, list)
        else []
    )
    media = _parse_media_references(items)
    message_type = _detect_content_type(items)
    timestamp = _parse_timestamp(raw.get("create_time_ms"))
    return IncomingMessage(
        upstream_id=_upstream_id(raw),
        user_id=user_id,
        context_token=context_token,
        text=_extract_text(items),
        type=message_type,
        timestamp=timestamp,
        media=tuple(media),
        quoted_message=_parse_quote(items),
    )


def _upstream_id(raw: dict[str, Any]) -> str:
    for key in ("message_id", "client_id", "seq"):
        value = raw.get(key)
        if isinstance(value, (str, int)) and not isinstance(value, bool):
            return f"{key}:{value}"
    canonical = json.dumps(
        raw,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    ).encode("utf-8")
    return f"digest:{hashlib.sha256(canonical).hexdigest()}"


def _parse_timestamp(value: object) -> datetime:
    if isinstance(value, (int, float)) and not isinstance(value, bool) and value >= 0:
        try:
            return datetime.fromtimestamp(value / 1000, tz=UTC)
        except (OSError, OverflowError, ValueError):
            pass
    return datetime.fromtimestamp(0, tz=UTC)


def _extract_text(items: list[dict[str, Any]]) -> str:
    """只抽取真实正文或语音转写，不人为生成 `[image]` 等占位符。"""

    parts: list[str] = []
    for item in items:
        item_type = item.get("type")
        if item_type == int(WireItemType.TEXT):
            text_item = item.get("text_item")
            if isinstance(text_item, dict):
                text = text_item.get("text")
                if isinstance(text, str) and text:
                    parts.append(text)
        elif item_type == int(WireItemType.VOICE):
            voice_item = item.get("voice_item")
            if isinstance(voice_item, dict):
                transcript = voice_item.get("text")
                if isinstance(transcript, str) and transcript:
                    parts.append(transcript)
    return "\n".join(parts)


def _parse_media_references(items: list[dict[str, Any]]) -> list[MediaReference]:
    references: list[MediaReference] = []
    for item in items:
        raw_type = item.get("type")
        kind = (
            _MEDIA_KIND_BY_ITEM_TYPE.get(raw_type)
            if isinstance(raw_type, int) and not isinstance(raw_type, bool)
            else None
        )
        if kind is None:
            continue
        detail_name = f"{kind.value}_item"
        detail = item.get(detail_name)
        if not isinstance(detail, dict):
            continue
        cdn = _parse_cdn_media(detail.get("media"))
        if cdn is None:
            continue
        aes_override = detail.get("aeskey") if kind is MediaKind.IMAGE else None
        references.append(
            MediaReference(
                kind=kind,
                cdn=cdn,
                aes_key_override=aes_override if isinstance(aes_override, str) else None,
                file_name=(
                    detail.get("file_name")
                    if kind is MediaKind.FILE and isinstance(detail.get("file_name"), str)
                    else None
                ),
                format=_voice_format(detail) if kind is MediaKind.VOICE else None,
            )
        )
    return references


def _detect_content_type(items: list[dict[str, Any]]) -> ContentType:
    """消息即使暂时缺少 CDN 引用，也必须保留其原始媒体类型。"""

    for item in items:
        raw_type = item.get("type")
        if isinstance(raw_type, int) and not isinstance(raw_type, bool):
            kind = _MEDIA_KIND_BY_ITEM_TYPE.get(raw_type)
            if kind is not None:
                return kind.value
    return "text"


def _parse_cdn_media(value: object) -> CdnMedia | None:
    if not isinstance(value, dict):
        return None
    encrypted_query_param = value.get("encrypt_query_param")
    full_url = value.get("full_url")
    if not isinstance(encrypted_query_param, str):
        encrypted_query_param = ""
    if not isinstance(full_url, str) or not full_url:
        full_url = None
    if not encrypted_query_param and full_url is None:
        return None
    aes_key = value.get("aes_key")
    encrypt_type = value.get("encrypt_type")
    return CdnMedia(
        encrypted_query_param=encrypted_query_param,
        aes_key=aes_key if isinstance(aes_key, str) else "",
        full_url=full_url,
        encrypt_type=encrypt_type if isinstance(encrypt_type, int) else None,
    )


def _voice_format(detail: dict[str, Any]) -> str:
    return "silk" if detail.get("encode_type") == 6 else "bin"


def _parse_quote(items: list[dict[str, Any]]) -> QuotedMessage | None:
    for item in items:
        raw_reference = item.get("ref_msg")
        if not isinstance(raw_reference, dict):
            continue
        raw_item = raw_reference.get("message_item")
        reference_item = raw_item if isinstance(raw_item, dict) else {}
        quote_type = _detect_content_type([reference_item])
        text = _extract_text([reference_item])
        if not text and quote_type == "file":
            file_detail = reference_item.get("file_item")
            if isinstance(file_detail, dict) and isinstance(file_detail.get("file_name"), str):
                text = file_detail["file_name"]
        title = raw_reference.get("title")
        normalized_title = title if isinstance(title, str) and title else None
        if normalized_title is not None or text or quote_type != "text":
            return QuotedMessage(
                title=normalized_title,
                text=text,
                type=quote_type,
            )
    return None
