"""聊天历史分页游标的版本化编码。"""

from __future__ import annotations

import base64
import binascii
import json
from datetime import UTC, datetime
from typing import Final

from ...domain.errors import InvalidCursorError
from ...domain.models import ConversationMessage

_CURSOR_VERSION: Final[int] = 1


def encode_conversation_cursor(message: ConversationMessage) -> str:
    """把分页位置编码为客户端不可依赖内部结构的 URL 安全值。"""

    payload = json.dumps(
        {
            "v": _CURSOR_VERSION,
            "occurred_at": _dump_time(message.occurred_at),
            "id": message.id,
        },
        separators=(",", ":"),
    ).encode("utf-8")
    return base64.urlsafe_b64encode(payload).decode("ascii").rstrip("=")


def decode_conversation_cursor(value: str) -> tuple[str, str]:
    """校验游标版本、时间和消息 ID，返回数据库可比较的值。"""

    try:
        padding = "=" * (-len(value) % 4)
        decoded = base64.b64decode(
            f"{value}{padding}",
            altchars=b"-_",
            validate=True,
        )
        payload = json.loads(decoded.decode("utf-8"))
        if not isinstance(payload, dict) or payload.get("v") != _CURSOR_VERSION:
            raise ValueError
        occurred_at = payload.get("occurred_at")
        message_id = payload.get("id")
        if not isinstance(occurred_at, str) or not isinstance(message_id, str):
            raise ValueError
        parsed_occurred_at = datetime.fromisoformat(occurred_at)
        if parsed_occurred_at.tzinfo is None or not message_id or len(message_id) > 128:
            raise ValueError
    except (binascii.Error, UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        raise InvalidCursorError("The conversation cursor is invalid") from exc
    return _dump_time(parsed_occurred_at), message_id


def _dump_time(value: datetime) -> str:
    return value.astimezone(UTC).isoformat(timespec="microseconds")
