"""同步消息应用服务测试，使用行为确定的内存微信网关替身。"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from pathlib import Path

import pytest

from wechat_bot_bridge.application.messaging import NotificationService
from wechat_bot_bridge.domain.errors import (
    GatewayNotReadyError,
    GatewayTemporaryError,
    OwnerNotBoundError,
    QuotedMessageNotFoundError,
)
from wechat_bot_bridge.domain.models import (
    ConversationMessage,
    ConversationStatus,
    GatewayPhase,
    GatewayStatus,
    ImagePayload,
    InboundEvent,
    InboundMessageBody,
    InboundSender,
    MessageOrigin,
    OutboundPayload,
    OutboundStatus,
    TextPayload,
)
from wechat_bot_bridge.infrastructure.persistence.sqlite import SQLiteStore

SESSION = "session-a"


class FakeGateway:
    def __init__(
        self,
        *,
        session_fingerprint: str | None = SESSION,
        outcomes: list[Exception | None] | None = None,
        delay: float = 0,
    ) -> None:
        self._session_fingerprint = session_fingerprint
        self._outcomes = list(outcomes or [])
        self._delay = delay
        self.calls: list[tuple[str, OutboundPayload, str]] = []
        self.active_sends = 0
        self.max_active_sends = 0

    @property
    def status(self) -> GatewayStatus:
        return GatewayStatus(
            phase=GatewayPhase.LISTENING,
            authenticated=True,
            listener_running=True,
        )

    @property
    def session_fingerprint(self) -> str | None:
        return self._session_fingerprint

    async def run(self) -> None:
        return None

    async def stop(self) -> None:
        return None

    async def send_message(
        self,
        *,
        user_id: str,
        payload: OutboundPayload,
        context_token: str,
    ) -> None:
        self.calls.append((user_id, payload, context_token))
        self.active_sends += 1
        self.max_active_sends = max(self.max_active_sends, self.active_sends)
        try:
            if self._delay:
                await asyncio.sleep(self._delay)
            if self._outcomes:
                outcome = self._outcomes.pop(0)
                if outcome is not None:
                    raise outcome
        finally:
            self.active_sends -= 1


class CapturePublisher:
    def __init__(self) -> None:
        self.messages: list[ConversationMessage] = []
        self.typing: list[tuple[bool, MessageOrigin]] = []

    async def publish_message(self, message: ConversationMessage) -> None:
        self.messages.append(message)

    async def publish_typing(
        self,
        *,
        active: bool,
        origin: MessageOrigin,
    ) -> None:
        self.typing.append((active, origin))


async def _store_with_owner(tmp_path: Path) -> SQLiteStore:
    store = SQLiteStore(tmp_path / "bridge.db")
    await store.open()
    await store.activate_session(SESSION)
    await store.save_owner_context(
        user_id="user-1",
        context_token="private-context",
        credential_fingerprint=SESSION,
    )
    return store


def _service(
    store: SQLiteStore,
    gateway: FakeGateway,
    publisher: CapturePublisher | None = None,
) -> NotificationService:
    return NotificationService(
        store=store,
        gateway=gateway,
        publisher=publisher or CapturePublisher(),
        message_max_chars=16_000,
        media_max_bytes=20 * 1024 * 1024,
    )


async def test_notification_service_requires_an_authorized_session(tmp_path: Path) -> None:
    store = SQLiteStore(tmp_path / "bridge.db")
    await store.open()
    service = _service(store, FakeGateway(session_fingerprint=None))
    try:
        with pytest.raises(GatewayNotReadyError):
            await service.send(
                payload=TextPayload(text="task complete"),
                origin=MessageOrigin.EXTERNAL_API,
            )
    finally:
        await store.close()


async def test_notification_service_rejects_unbound_owner(tmp_path: Path) -> None:
    store = SQLiteStore(tmp_path / "bridge.db")
    await store.open()
    await store.activate_session(SESSION)
    service = _service(store, FakeGateway())
    try:
        with pytest.raises(OwnerNotBoundError):
            await service.send(
                payload=TextPayload(text="task complete"),
                origin=MessageOrigin.EXTERNAL_API,
            )
    finally:
        await store.close()


async def test_each_request_is_distinct_and_publishes_sending_then_sent(
    tmp_path: Path,
) -> None:
    store = await _store_with_owner(tmp_path)
    gateway = FakeGateway()
    publisher = CapturePublisher()
    service = _service(store, gateway, publisher)
    try:
        first = await service.send(
            payload=TextPayload(text="task complete"),
            origin=MessageOrigin.EXTERNAL_API,
        )
        second = await service.send(
            payload=TextPayload(text="task complete"),
            origin=MessageOrigin.EXTERNAL_API,
        )

        assert first.id != second.id
        assert first.status is OutboundStatus.SENT
        assert second.status is OutboundStatus.SENT
        assert first.sent_at is not None
        assert [message.status for message in publisher.messages] == [
            ConversationStatus.SENDING,
            ConversationStatus.SENT,
            ConversationStatus.SENDING,
            ConversationStatus.SENT,
        ]
        assert gateway.calls == [
            ("user-1", TextPayload(text="task complete"), "private-context"),
            ("user-1", TextPayload(text="task complete"), "private-context"),
        ]
    finally:
        await store.close()


async def test_gateway_failure_is_returned_immediately_and_published(
    tmp_path: Path,
) -> None:
    store = await _store_with_owner(tmp_path)
    gateway = FakeGateway(outcomes=[GatewayTemporaryError("temporary")])
    publisher = CapturePublisher()
    service = _service(store, gateway, publisher)
    try:
        with pytest.raises(GatewayTemporaryError, match="temporary"):
            await service.send(
                payload=TextPayload(text="task complete"),
                origin=MessageOrigin.EXTERNAL_API,
            )

        page = await store.list_conversation_messages(cursor=None, limit=10)
        assert len(page.items) == 1
        assert page.items[0].status is ConversationStatus.FAILED
        assert page.items[0].error_code == "wechat_temporary_error"
        assert [message.status for message in publisher.messages] == [
            ConversationStatus.SENDING,
            ConversationStatus.FAILED,
        ]
    finally:
        await store.close()


async def test_concurrent_requests_are_serialized_per_wechat_account(
    tmp_path: Path,
) -> None:
    store = await _store_with_owner(tmp_path)
    gateway = FakeGateway(delay=0.01)
    service = _service(store, gateway)
    try:
        results = await asyncio.gather(
            *(
                service.send(
                    payload=TextPayload(text=f"message {index}"),
                    origin=MessageOrigin.WEB_ADMIN,
                )
                for index in range(5)
            )
        )

        assert all(message.status is OutboundStatus.SENT for message in results)
        assert gateway.max_active_sends == 1
    finally:
        await store.close()


async def test_payload_validation_happens_before_creating_chat_record(
    tmp_path: Path,
) -> None:
    store = await _store_with_owner(tmp_path)
    service = NotificationService(
        store=store,
        gateway=FakeGateway(),
        publisher=CapturePublisher(),
        message_max_chars=5,
        media_max_bytes=10,
    )
    try:
        with pytest.raises(ValueError, match="character limit"):
            await service.send(
                payload=TextPayload(text="123456"),
                origin=MessageOrigin.EXTERNAL_API,
            )
        with pytest.raises(ValueError, match="byte limit"):
            await service.send(
                payload=ImagePayload(data=b"x" * 11),
                origin=MessageOrigin.EXTERNAL_API,
            )

        page = await store.list_conversation_messages(cursor=None, limit=10)
        assert page.items == ()
    finally:
        await store.close()


async def test_bridge_quote_is_persisted_and_explicit_prefix_is_sent(
    tmp_path: Path,
) -> None:
    store = await _store_with_owner(tmp_path)
    gateway = FakeGateway()
    service = _service(store, gateway)
    inbound = InboundEvent(
        event_id="incoming-to-quote",
        occurred_at=datetime.now(UTC),
        sender=InboundSender(user_id="user-1"),
        message=InboundMessageBody(type="text", text="原始微信消息"),
    )
    try:
        await store.record_inbound_conversation(inbound)
        sent = await service.send(
            payload=TextPayload(text="这是回复"),
            origin=MessageOrigin.WEB_ADMIN,
            quote_message_id=inbound.event_id,
        )

        assert gateway.calls == [
            (
                "user-1",
                TextPayload(text="【桥接引用·对方】原始微信消息\n这是回复"),
                "private-context",
            )
        ]
        stored = await store.get_conversation_message(sent.id)
        assert stored is not None
        assert stored.text == "这是回复"
        assert stored.quote is not None
        assert stored.quote.id == inbound.event_id
        assert stored.quote.text == "原始微信消息"
    finally:
        await store.close()


async def test_missing_bridge_quote_is_rejected_before_creating_message(
    tmp_path: Path,
) -> None:
    store = await _store_with_owner(tmp_path)
    gateway = FakeGateway()
    service = _service(store, gateway)
    try:
        with pytest.raises(QuotedMessageNotFoundError):
            await service.send(
                payload=TextPayload(text="不会发送"),
                origin=MessageOrigin.WEB_ADMIN,
                quote_message_id="missing-message",
            )

        assert gateway.calls == []
        assert (await store.list_conversation_messages(cursor=None, limit=10)).items == ()
    finally:
        await store.close()


async def test_bridge_quote_does_not_duplicate_media_placeholder(tmp_path: Path) -> None:
    store = await _store_with_owner(tmp_path)
    gateway = FakeGateway()
    service = _service(store, gateway)
    inbound = InboundEvent(
        event_id="incoming-image-to-quote",
        occurred_at=datetime.now(UTC),
        sender=InboundSender(user_id="user-1"),
        message=InboundMessageBody(type="image", text="[图片]"),
    )
    try:
        await store.record_inbound_conversation(inbound)
        await service.send(
            payload=TextPayload(text="收到"),
            origin=MessageOrigin.WEB_ADMIN,
            quote_message_id=inbound.event_id,
        )

        assert gateway.calls[0][1] == TextPayload(text="【桥接引用·对方】[图片]\n收到")
    finally:
        await store.close()
