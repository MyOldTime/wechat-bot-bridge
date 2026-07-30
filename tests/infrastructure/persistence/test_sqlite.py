"""SQLite 存储测试，每个测试使用独立数据库。"""

from __future__ import annotations

import asyncio
import sqlite3
from datetime import timedelta
from pathlib import Path

import pytest

from wechat_bot_bridge.domain.errors import InvalidCursorError
from wechat_bot_bridge.domain.models import (
    ConversationDirection,
    ConversationMessageType,
    ConversationStatus,
    ImagePayload,
    InboundEvent,
    InboundMedia,
    InboundMessageBody,
    InboundQuote,
    InboundSender,
    MessageOrigin,
    OutboundMessage,
    OutboundMessageType,
    OutboundStatus,
    TextPayload,
)
from wechat_bot_bridge.infrastructure.persistence.sqlite import SQLiteStore, utc_now


async def _open_store(path: Path) -> SQLiteStore:
    store = SQLiteStore(path)
    await store.open()
    return store


async def _begin_text(
    store: SQLiteStore,
    *,
    text: str = "hello",
    user_id: str = "user-1",
    origin: MessageOrigin = MessageOrigin.EXTERNAL_API,
) -> OutboundMessage:
    return await store.begin_outbound(
        user_id=user_id,
        message_type=OutboundMessageType.TEXT,
        payload=TextPayload(text=text),
        origin=origin,
    )


async def test_store_persists_state_and_owner_context_across_reopen(
    tmp_path: Path,
) -> None:
    path = tmp_path / "state" / "bridge.db"
    store = await _open_store(path)
    await store.activate_session("session-a")
    await store.set_state("cursor:session-a", "cursor-42")
    await store.save_owner_context(
        user_id="user-1",
        context_token="private-context-token",
        credential_fingerprint="session-a",
    )
    await store.close()

    reopened = await _open_store(path)
    try:
        assert await reopened.get_state("cursor:session-a") == "cursor-42"
        owner = await reopened.resolve_owner("session-a")
        assert owner is not None
        assert owner.user_id == "user-1"
        assert owner.context_token == "private-context-token"
    finally:
        await reopened.close()


async def test_identical_outbound_submissions_create_distinct_internal_ids(
    tmp_path: Path,
) -> None:
    store = await _open_store(tmp_path / "bridge.db")
    try:
        first = await _begin_text(store)
        repeated = await _begin_text(store)

        assert repeated.id != first.id
        assert repeated.status is OutboundStatus.SENDING

        connection = store._require_connection()
        cursor = await connection.execute(
            """
            SELECT idempotency_key, payload_hash
              FROM outbound_messages
             ORDER BY rowid
            """
        )
        rows = await cursor.fetchall()
        await cursor.close()
        assert [str(row["idempotency_key"]) for row in rows] == [
            f"internal:{first.id}",
            f"internal:{repeated.id}",
        ]
        assert all(str(row["payload_hash"]) == "" for row in rows)
    finally:
        await store.close()


async def test_concurrent_creates_remain_distinct(tmp_path: Path) -> None:
    store = await _open_store(tmp_path / "bridge.db")

    async def begin() -> str:
        return (await _begin_text(store)).id

    try:
        results = await asyncio.gather(*(begin() for _ in range(8)))
        assert len(set(results)) == 8
    finally:
        await store.close()


async def test_inbound_event_deduplication_and_chat_record(tmp_path: Path) -> None:
    store = await _open_store(tmp_path / "bridge.db")
    event = InboundEvent(
        event_id="event-1",
        occurred_at=utc_now(),
        sender=InboundSender(user_id="user-1"),
        message=InboundMessageBody(type="text", text="微信发来的内容"),
    )
    try:
        assert await store.remember_inbound_event(event) is True
        assert await store.remember_inbound_event(event) is False

        message = await store.record_inbound_conversation(event)
        duplicate = await store.record_inbound_conversation(event)
        assert duplicate == message
        assert message.direction is ConversationDirection.INBOUND
        assert message.status is ConversationStatus.RECEIVED
        assert message.origin is MessageOrigin.WECHAT
        assert message.text == "微信发来的内容"
    finally:
        await store.close()


async def test_owner_context_is_singleton_and_only_same_owner_can_refresh(
    tmp_path: Path,
) -> None:
    store = await _open_store(tmp_path / "bridge.db")
    try:
        await store.activate_session("session-a")
        await store.save_owner_context(
            user_id="user-1",
            context_token="private-token-1",
            credential_fingerprint="session-a",
        )
        await store.save_owner_context(
            user_id="user-1",
            context_token="private-token-2",
            credential_fingerprint="session-a",
        )

        owner = await store.resolve_owner("session-a")
        assert owner is not None
        assert owner.user_id == "user-1"
        assert owner.context_token == "private-token-2"

        with pytest.raises(RuntimeError, match="Owner binding conflicts"):
            await store.save_owner_context(
                user_id="user-2",
                context_token="untrusted-token",
                credential_fingerprint="session-a",
            )
    finally:
        await store.close()


async def test_reconcile_owner_binding_promotes_matching_legacy_context(
    tmp_path: Path,
) -> None:
    store = await _open_store(tmp_path / "bridge.db")
    try:
        await store.activate_session("session-a")
        connection = store._require_connection()
        await connection.execute(
            """
            INSERT INTO recipients(
                alias, user_id, context_token, credential_fingerprint, updated_at
            ) VALUES(?, ?, ?, ?, ?)
            """,
            (
                "user-1",
                "user-1",
                "legacy-private-token",
                "session-a",
                utc_now().isoformat(),
            ),
        )
        await connection.commit()

        assert await store.reconcile_owner_binding(
            user_id="user-1",
            credential_fingerprint="session-a",
        )
        owner = await store.resolve_owner("session-a")
        assert owner is not None
        assert owner.context_token == "legacy-private-token"
    finally:
        await store.close()


async def test_session_change_invalidates_outbound_and_chat_records(
    tmp_path: Path,
) -> None:
    store = await _open_store(tmp_path / "bridge.db")
    try:
        assert await store.activate_session("session-a") is False
        await store.save_owner_context(
            user_id="user-1",
            context_token="private-token",
            credential_fingerprint="session-a",
        )
        outbound = await _begin_text(store)

        assert await store.activate_session("session-b") is True
        assert await store.resolve_owner("session-b") is None
        invalidated = await store.get_outbound(outbound.id)
        chat = await store.get_conversation_message(outbound.id)
        assert invalidated is not None
        assert invalidated.status is OutboundStatus.FAILED
        assert invalidated.last_error_code == "interaction_required"
        assert chat is not None
        assert chat.status is ConversationStatus.FAILED
        assert chat.error_code == "interaction_required"
    finally:
        await store.close()


async def test_interrupted_synchronous_send_is_failed_after_restart(
    tmp_path: Path,
) -> None:
    path = tmp_path / "bridge.db"
    store = await _open_store(path)
    outbound = await _begin_text(store)
    await store.close()

    reopened = await _open_store(path)
    try:
        recovered = await reopened.get_outbound(outbound.id)
        chat = await reopened.get_conversation_message(outbound.id)
        assert recovered is not None
        assert recovered.status is OutboundStatus.FAILED
        assert recovered.last_error_code == "delivery_interrupted"
        assert chat is not None
        assert chat.status is ConversationStatus.FAILED
    finally:
        await reopened.close()


async def test_legacy_outbox_schema_is_migrated_without_resending(
    tmp_path: Path,
) -> None:
    path = tmp_path / "legacy.db"
    now = utc_now().isoformat()
    connection = sqlite3.connect(path)
    try:
        connection.executescript(
            """
            CREATE TABLE outbound_messages (
                id TEXT PRIMARY KEY,
                idempotency_key TEXT NOT NULL UNIQUE,
                payload_hash TEXT NOT NULL,
                recipient TEXT NOT NULL,
                user_id TEXT NOT NULL,
                text TEXT NOT NULL,
                status TEXT NOT NULL,
                attempts INTEGER NOT NULL DEFAULT 0,
                next_attempt_at TEXT NOT NULL,
                last_error_code TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                sent_at TEXT
            );
            """
        )
        connection.execute(
            """
            INSERT INTO outbound_messages(
                id, idempotency_key, payload_hash, recipient, user_id, text,
                status, attempts, next_attempt_at, created_at, updated_at
            ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "legacy-message",
                "legacy-idempotency",
                "legacy-hash",
                "owner",
                "user-1",
                "legacy text",
                "queued",
                0,
                now,
                now,
                now,
            ),
        )
        connection.commit()
    finally:
        connection.close()

    store = await _open_store(path)
    try:
        migrated = await store.get_outbound("legacy-message")
        assert migrated is not None
        assert migrated.message_type is OutboundMessageType.TEXT
        assert migrated.status is OutboundStatus.FAILED
        assert migrated.last_error_code == "delivery_interrupted"

        database = store._require_connection()
        cursor = await database.execute("PRAGMA table_info(outbound_messages)")
        columns = {str(row["name"]) for row in await cursor.fetchall()}
        await cursor.close()
        assert "message_type" in columns
    finally:
        await store.close()


async def test_chat_history_contains_content_and_uses_stable_cursor(
    tmp_path: Path,
) -> None:
    store = await _open_store(tmp_path / "bridge.db")
    submitted: list[OutboundMessage] = []
    try:
        for index in range(5):
            message = await _begin_text(
                store,
                text=f"完整消息内容 {index}",
                origin=MessageOrigin.WEB_ADMIN if index % 2 else MessageOrigin.EXTERNAL_API,
            )
            submitted.append(message)
            if index == 2:
                await store.mark_outbound_failed(message.id, "test_failure")
            else:
                await store.mark_outbound_sent(message.id)

        first = await store.list_conversation_messages(cursor=None, limit=2)
        second = await store.list_conversation_messages(cursor=first.next_cursor, limit=2)
        third = await store.list_conversation_messages(cursor=second.next_cursor, limit=2)

        assert first.next_cursor is not None
        assert second.next_cursor is not None
        assert third.next_cursor is None
        all_items = first.items + second.items + third.items
        assert {message.id for message in all_items} == {message.id for message in submitted}
        assert {message.text for message in all_items} == {
            f"完整消息内容 {index}" for index in range(5)
        }

        with pytest.raises(InvalidCursorError):
            await store.list_conversation_messages(cursor="invalid", limit=10)
    finally:
        await store.close()


async def test_image_media_is_stored_and_returned_separately(tmp_path: Path) -> None:
    store = await _open_store(tmp_path / "bridge.db")
    png = b"\x89PNG\r\n\x1a\npayload"
    try:
        outbound = await store.begin_outbound(
            user_id="user-1",
            message_type=OutboundMessageType.IMAGE,
            payload=ImagePayload(data=png, caption="运行截图"),
            origin=MessageOrigin.WEB_ADMIN,
        )
        message = await store.get_conversation_message(outbound.id)
        media = await store.get_conversation_media(outbound.id)

        assert message is not None
        assert message.message_type is ConversationMessageType.IMAGE
        assert message.text == "运行截图"
        assert message.has_media is True
        assert media is not None
        assert media.data == png
        assert media.mime_type == "image/png"
    finally:
        await store.close()


async def test_cursor_orders_equal_timestamps_by_message_id(tmp_path: Path) -> None:
    store = await _open_store(tmp_path / "bridge.db")
    event_time = utc_now() - timedelta(minutes=1)
    try:
        for event_id in ("event-c", "event-a", "event-b"):
            await store.record_inbound_conversation(
                InboundEvent(
                    event_id=event_id,
                    occurred_at=event_time,
                    sender=InboundSender(user_id="user-1"),
                    message=InboundMessageBody(type="text", text=event_id),
                )
            )
        first = await store.list_conversation_messages(cursor=None, limit=2)
        second = await store.list_conversation_messages(cursor=first.next_cursor, limit=2)
        assert [message.id for message in first.items] == ["event-b", "event-c"]
        assert [message.id for message in second.items] == ["event-a"]
    finally:
        await store.close()


async def test_legacy_bridge_quote_relation_is_not_returned(tmp_path: Path) -> None:
    store = await _open_store(tmp_path / "bridge.db")
    try:
        database = store._require_connection()
        await database.execute("ALTER TABLE chat_messages ADD COLUMN quote_message_id TEXT")
        original = await _begin_text(store, text="旧版原消息")
        reply = await _begin_text(store, text="旧版桥接引用回复")
        await database.execute(
            "UPDATE chat_messages SET quote_message_id = ? WHERE id = ?",
            (original.id, reply.id),
        )
        await database.commit()

        conversation = await store.get_conversation_message(reply.id)
        assert conversation is not None
        assert conversation.text == "旧版桥接引用回复"
        assert conversation.quote is None
    finally:
        await store.close()


async def test_inbound_downloaded_media_is_available_from_protected_store(
    tmp_path: Path,
) -> None:
    store = await _open_store(tmp_path / "bridge.db")
    png = b"\x89PNG\r\n\x1a\ninbound-image"
    event = InboundEvent(
        event_id="inbound-image",
        occurred_at=utc_now(),
        sender=InboundSender(user_id="user-1"),
        message=InboundMessageBody(type="image", text="[图片]"),
        media=InboundMedia(data=png, mime_type="image/png"),
    )
    try:
        message = await store.record_inbound_conversation(event)
        media = await store.get_conversation_media(event.event_id)

        assert message.message_type is ConversationMessageType.IMAGE
        assert message.has_media is True
        assert message.mime_type == "image/png"
        assert media is not None
        assert media.data == png
        assert media.file_name is None
    finally:
        await store.close()


async def test_inbound_native_quote_snapshot_is_persisted_without_fake_target_id(
    tmp_path: Path,
) -> None:
    store = await _open_store(tmp_path / "bridge.db")
    event = InboundEvent(
        event_id="native-quote",
        occurred_at=utc_now(),
        sender=InboundSender(user_id="user-1"),
        message=InboundMessageBody(type="text", text="引用后的新内容"),
        quote=InboundQuote(title="微信桥接", text="瓷器"),
    )
    try:
        message = await store.record_inbound_conversation(event)

        assert message.quote is not None
        assert message.quote.title == "微信桥接"
        assert message.quote.text == "瓷器"
        assert message.quote.message_type is ConversationMessageType.TEXT
        assert message.quote.target_message_id is None
    finally:
        await store.close()


async def test_inbound_quote_item_id_recovers_original_text_and_target(
    tmp_path: Path,
) -> None:
    store = await _open_store(tmp_path / "bridge.db")
    original_time = utc_now()
    original = InboundEvent(
        event_id="original-message",
        occurred_at=original_time,
        sender=InboundSender(user_id="user-1"),
        message=InboundMessageBody(type="text", text="测试"),
        protocol_item_id="wx-item-original",
    )
    reply = InboundEvent(
        event_id="quoted-reply",
        occurred_at=original_time + timedelta(seconds=1),
        sender=InboundSender(user_id="user-1"),
        message=InboundMessageBody(type="text", text="11"),
        protocol_item_id="wx-item-reply",
        quote=InboundQuote(
            title=None,
            text="",
            protocol_item_id="wx-item-original",
        ),
    )
    try:
        await store.record_inbound_conversation(original)
        message = await store.record_inbound_conversation(reply)
        page = await store.list_conversation_messages(cursor=None, limit=10)

        assert message.quote is not None
        assert message.quote.target_message_id == original.event_id
        assert message.quote.message_type is ConversationMessageType.TEXT
        assert message.quote.text == "测试"
        assert page.items[-1].quote == message.quote
    finally:
        await store.close()


async def test_inbound_quote_item_id_matches_original_top_level_message_id(
    tmp_path: Path,
) -> None:
    """微信引用微信消息时，引用项 ID 可以指向原消息的顶层 message_id。"""

    store = await _open_store(tmp_path / "bridge.db")
    original_time = utc_now()
    original = InboundEvent(
        event_id="wechat-original",
        occurred_at=original_time,
        sender=InboundSender(user_id="user-1"),
        message=InboundMessageBody(type="text", text="你好"),
        protocol_item_id="original-current-item",
        protocol_message_id="quoted-reference-id",
    )
    reply = InboundEvent(
        event_id="wechat-reply",
        occurred_at=original_time + timedelta(seconds=1),
        sender=InboundSender(user_id="user-1"),
        message=InboundMessageBody(type="text", text="测试引用"),
        protocol_item_id="reply-current-item",
        protocol_message_id="reply-top-level-message",
        quote=InboundQuote(
            title=None,
            text="",
            protocol_item_id="quoted-reference-id",
        ),
    )
    try:
        await store.record_inbound_conversation(original)
        message = await store.record_inbound_conversation(reply)

        assert message.quote is not None
        assert message.quote.target_message_id == original.event_id
        assert message.quote.text == "你好"
    finally:
        await store.close()


async def test_legacy_chat_schema_adds_native_quote_snapshot_columns(tmp_path: Path) -> None:
    path = tmp_path / "legacy-chat.db"
    connection = sqlite3.connect(path)
    try:
        connection.executescript(
            """
            CREATE TABLE chat_messages (
                id TEXT PRIMARY KEY,
                direction TEXT NOT NULL,
                message_type TEXT NOT NULL,
                text TEXT NOT NULL DEFAULT '',
                file_name TEXT,
                mime_type TEXT,
                media BLOB,
                status TEXT NOT NULL,
                origin TEXT NOT NULL,
                error_code TEXT,
                occurred_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            """
        )
        connection.commit()
    finally:
        connection.close()

    store = await _open_store(path)
    try:
        database = store._require_connection()
        cursor = await database.execute("PRAGMA table_info(chat_messages)")
        columns = {str(row["name"]) for row in await cursor.fetchall()}
        await cursor.close()
        assert {
            "protocol_item_id",
            "protocol_message_id",
            "native_quote_title",
            "native_quote_type",
            "native_quote_text",
            "native_quote_target_id",
            "native_quote_protocol_item_id",
        } <= columns
        assert "quote_message_id" not in columns
    finally:
        await store.close()


async def test_clear_all_removes_every_business_table_in_one_reset(
    tmp_path: Path,
) -> None:
    store = await _open_store(tmp_path / "bridge.db")
    event = InboundEvent(
        event_id="event-to-clear",
        occurred_at=utc_now(),
        sender=InboundSender(user_id="owner-user"),
        message=InboundMessageBody(type="text", text="需要清除的聊天记录"),
    )
    try:
        await store.activate_session("session-to-clear")
        await store.set_state("cursor:session-to-clear", "private-cursor")
        await store.save_owner_context(
            user_id="owner-user",
            context_token="private-context",
            credential_fingerprint="session-to-clear",
        )
        assert await store.remember_inbound_event(event) is True
        await store.record_inbound_conversation(event)
        await _begin_text(store, text="需要清除的出站消息", user_id="owner-user")

        await store.clear_all()

        connection = store._require_connection()
        for table in (
            "bridge_state",
            "recipients",
            "inbound_events",
            "outbound_messages",
            "chat_messages",
        ):
            cursor = await connection.execute(f"SELECT COUNT(*) AS total FROM {table}")
            row = await cursor.fetchone()
            await cursor.close()
            assert row is not None
            assert int(row["total"]) == 0
        assert await store.remember_inbound_event(event) is True
    finally:
        await store.close()
