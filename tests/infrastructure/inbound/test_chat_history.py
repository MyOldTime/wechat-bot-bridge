"""聊天记录 Sink 与实时广播测试。"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from wechat_bot_bridge.domain.models import (
    ConversationDirection,
    InboundEvent,
    InboundMessageBody,
    InboundSender,
)
from wechat_bot_bridge.infrastructure.inbound.chat_history import ChatHistorySink
from wechat_bot_bridge.infrastructure.persistence.sqlite import SQLiteStore
from wechat_bot_bridge.infrastructure.realtime.conversation import (
    ConversationHub,
    MessageBroadcast,
    ResetBroadcast,
)


async def test_chat_history_sink_persists_and_broadcasts_inbound_message(
    tmp_path: Path,
) -> None:
    store = SQLiteStore(tmp_path / "bridge.db")
    await store.open()
    hub = ConversationHub()
    sink = ChatHistorySink(store=store, publisher=hub)
    event = InboundEvent(
        event_id="event-1",
        occurred_at=datetime(2026, 7, 29, 12, 0, tzinfo=UTC),
        sender=InboundSender(user_id="owner-id-must-not-be-exposed"),
        message=InboundMessageBody(type="text", text="来自微信的消息"),
    )

    try:
        async with hub.subscribe() as queue:
            await sink.deliver(event)
            broadcast = await queue.get()

        assert isinstance(broadcast, MessageBroadcast)
        assert broadcast.message.direction is ConversationDirection.INBOUND
        assert broadcast.message.text == "来自微信的消息"
        assert "owner-id-must-not-be-exposed" not in repr(broadcast.message)

        page = await store.list_conversation_messages(cursor=None, limit=10)
        assert page.items == (broadcast.message,)
    finally:
        await sink.close()
        await store.close()


async def test_conversation_hub_drops_only_oldest_event_for_slow_subscriber(
    tmp_path: Path,
) -> None:
    store = SQLiteStore(tmp_path / "bridge.db")
    await store.open()
    hub = ConversationHub(subscriber_queue_size=1)
    sink = ChatHistorySink(store=store, publisher=hub)

    def event(number: int) -> InboundEvent:
        return InboundEvent(
            event_id=f"event-{number}",
            occurred_at=datetime(2026, 7, 29, 12, number, tzinfo=UTC),
            sender=InboundSender(user_id="owner"),
            message=InboundMessageBody(type="text", text=f"message {number}"),
        )

    try:
        async with hub.subscribe() as queue:
            await sink.deliver(event(1))
            await sink.deliver(event(2))
            latest = await queue.get()

        assert isinstance(latest, MessageBroadcast)
        assert latest.message.id == "event-2"
    finally:
        await store.close()


async def test_conversation_reset_discards_all_queued_pre_logout_events(
    tmp_path: Path,
) -> None:
    store = SQLiteStore(tmp_path / "bridge.db")
    await store.open()
    hub = ConversationHub(subscriber_queue_size=3)
    sink = ChatHistorySink(store=store, publisher=hub)
    first = InboundEvent(
        event_id="event-before-reset-1",
        occurred_at=datetime(2026, 7, 29, 12, 0, tzinfo=UTC),
        sender=InboundSender(user_id="owner"),
        message=InboundMessageBody(type="text", text="旧消息一"),
    )
    second = first.model_copy(
        update={
            "event_id": "event-before-reset-2",
            "message": InboundMessageBody(type="text", text="旧消息二"),
        }
    )

    try:
        async with hub.subscribe() as queue:
            await sink.deliver(first)
            await sink.deliver(second)
            await hub.publish_reset()

            reset = await queue.get()
            assert isinstance(reset, ResetBroadcast)
            assert queue.empty()
    finally:
        await store.close()
