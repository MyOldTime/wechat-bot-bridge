"""入站分发队列排空语义测试。"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime

from wechat_bot_bridge.domain.models import (
    InboundEvent,
    InboundMessageBody,
    InboundSender,
)
from wechat_bot_bridge.infrastructure.inbound.dispatcher import InboundDispatcher


class BlockingSink:
    def __init__(self) -> None:
        self.started = asyncio.Event()
        self.release = asyncio.Event()
        self.closed = False

    async def deliver(self, event: InboundEvent) -> None:
        assert event.event_id == "queued-before-logout"
        self.started.set()
        await self.release.wait()

    async def close(self) -> None:
        self.closed = True


async def test_drain_waits_until_inflight_event_finishes() -> None:
    sink = BlockingSink()
    dispatcher = InboundDispatcher([sink], queue_size=2)
    await dispatcher.start()
    event = InboundEvent(
        event_id="queued-before-logout",
        occurred_at=datetime(2026, 7, 29, 12, 0, tzinfo=UTC),
        sender=InboundSender(user_id="owner"),
        message=InboundMessageBody(type="text", text="退出前已接收"),
    )

    try:
        await dispatcher.publish(event)
        await sink.started.wait()
        draining = asyncio.create_task(dispatcher.drain())
        await asyncio.sleep(0)
        assert not draining.done()

        sink.release.set()
        await asyncio.wait_for(draining, timeout=1)
    finally:
        await dispatcher.stop()

    assert sink.closed is True
