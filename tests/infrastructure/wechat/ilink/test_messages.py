"""iLink 消息构造与解析测试。"""

from __future__ import annotations

import pytest

from wechat_bot_bridge.infrastructure.wechat.ilink.errors import ILinkProtocolError
from wechat_bot_bridge.infrastructure.wechat.ilink.messages import (
    build_message,
    build_text_item,
    chunk_text,
    parse_update_batch,
)


def test_parser_preserves_image_caption_without_creating_placeholder() -> None:
    batch = parse_update_batch(
        {
            "get_updates_buf": "cursor-2",
            "longpolling_timeout_ms": 35_000,
            "msgs": [
                {
                    "message_id": 1001,
                    "from_user_id": "owner",
                    "message_type": 1,
                    "create_time_ms": 1_785_283_200_000,
                    "context_token": "private-context",
                    "item_list": [
                        {"type": 1, "text_item": {"text": "图片说明"}},
                        {
                            "type": 2,
                            "image_item": {
                                "url": "https://example.invalid/legacy-image",
                                "media": {
                                    "encrypt_query_param": "encrypted-reference",
                                    "aes_key": "ZmFrZQ==",
                                },
                            },
                        },
                    ],
                }
            ],
        },
        current_cursor="cursor-1",
    )

    assert batch.cursor == "cursor-2"
    assert batch.suggested_timeout_ms == 35_000
    assert len(batch.messages) == 1
    message = batch.messages[0]
    assert message.upstream_id == "message_id:1001"
    assert message.message_id == "1001"
    assert message.type == "image"
    assert message.text == "图片说明"
    assert "[image]" not in message.text
    assert "legacy-image" not in message.text
    assert len(message.media) == 1


def test_parser_preserves_native_quote_type_title_and_text() -> None:
    batch = parse_update_batch(
        {
            "msgs": [
                {
                    "message_id": 1002,
                    "from_user_id": "owner",
                    "message_type": 1,
                    "context_token": "private-context",
                    "item_list": [
                        {
                            "type": 1,
                            "msg_id": "current-item-1002",
                            "text_item": {"text": "测试"},
                            "ref_msg": {
                                "title": "微信桥接",
                                "message_item": {
                                    "type": 1,
                                    "msg_id": "quoted-item-1001",
                                    "text_item": {"text": "瓷器"},
                                },
                            },
                        }
                    ],
                }
            ]
        },
        current_cursor="",
    )

    message = batch.messages[0]
    quote = message.quoted_message
    assert message.text == "测试"
    assert message.message_id == "1002"
    assert message.item_id == "current-item-1002"
    assert quote is not None
    assert quote.title == "微信桥接"
    assert quote.type == "text"
    assert quote.text == "瓷器"
    assert quote.item_id == "quoted-item-1001"


def test_parser_handles_file_quote_and_ignores_bot_messages() -> None:
    batch = parse_update_batch(
        {
            "msgs": [
                {
                    "message_id": 2001,
                    "client_id": "local-message-2001",
                    "to_user_id": "owner",
                    "message_type": 2,
                    "item_list": [
                        {
                            "type": 1,
                            "msg_id": "server-item-2001",
                            "text_item": {"text": "bot echo"},
                        }
                    ],
                },
                {
                    "client_id": "client-1",
                    "from_user_id": "owner",
                    "message_type": 1,
                    "context_token": "context",
                    "item_list": [
                        {
                            "type": 1,
                            "text_item": {"text": "收到"},
                            "ref_msg": {
                                "message_item": {
                                    "type": 4,
                                    "file_item": {"file_name": "report.pdf"},
                                }
                            },
                        }
                    ],
                },
            ]
        },
        current_cursor="cursor",
    )

    assert len(batch.messages) == 1
    message = batch.messages[0]
    assert message.upstream_id == "client_id:client-1"
    assert message.quoted_message is not None
    assert message.quoted_message.type == "file"
    assert message.quoted_message.text == "report.pdf"


def test_parser_preserves_partial_native_quote_marker() -> None:
    batch = parse_update_batch(
        {
            "msgs": [
                {
                    "message_id": 2002,
                    "from_user_id": "owner",
                    "message_type": 1,
                    "context_token": "context",
                    "item_list": [
                        {
                            "type": 1,
                            "msg_id": "current-item-2002",
                            "text_item": {"text": "引用图片后的正文"},
                            "ref_msg": {
                                "message_item": {
                                    "msg_id": "quoted-item-2001",
                                }
                            },
                        }
                    ],
                }
            ]
        },
        current_cursor="cursor",
    )

    message = batch.messages[0]
    quote = message.quoted_message
    assert message.item_id == "current-item-2002"
    assert quote is not None
    assert quote.title is None
    assert quote.type == "text"
    assert quote.text == ""
    assert quote.item_id == "quoted-item-2001"


def test_parser_rejects_invalid_cursor_or_message_list() -> None:
    with pytest.raises(ILinkProtocolError):
        parse_update_batch({"get_updates_buf": 123}, current_cursor="")
    with pytest.raises(ILinkProtocolError):
        parse_update_batch({"msgs": {}}, current_cursor="")


def test_text_chunking_prefers_natural_boundaries_and_builds_wire_message() -> None:
    chunks = chunk_text("第一段\n\n第二段内容", limit=6)
    assert "".join(chunks) == "第一段\n\n第二段内容"
    assert all(0 < len(chunk) <= 6 for chunk in chunks)

    message = build_message(
        message_id="local-message-1",
        user_id="owner",
        context_token="private-context",
        item=build_text_item("hello"),
    )
    assert message["to_user_id"] == "owner"
    assert message["context_token"] == "private-context"
    assert message["message_type"] == 2
    assert message["message_state"] == 2
    assert message["item_list"] == [{"type": 1, "text_item": {"text": "hello"}}]
    assert "ref_msg" not in message["item_list"][0]
    assert message["client_id"] == "local-message-1"
