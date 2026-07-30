"""管理页面输入状态租约测试。"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime

from wechat_bot_bridge.application.typing import TypingLeaseService
from wechat_bot_bridge.domain.models import (
    MessageOrigin,
    OutboundMessage,
    OutboundPayload,
    OutboundStatus,
    TypingCancelPayload,
    TypingStartPayload,
    outbound_payload_type,
)


class CaptureNotifications:
    def __init__(self) -> None:
        self.payloads: list[OutboundPayload] = []

    async def send(
        self,
        *,
        payload: OutboundPayload,
        origin: MessageOrigin,
    ) -> OutboundMessage:
        assert origin is MessageOrigin.WEB_ADMIN
        self.payloads.append(payload)
        now = datetime.now(UTC)
        return OutboundMessage(
            id=f"message-{len(self.payloads)}",
            user_id="owner-user",
            message_type=outbound_payload_type(payload),
            status=OutboundStatus.SENT,
            last_error_code=None,
            created_at=now,
            updated_at=now,
            sent_at=now,
        )


async def test_typing_lease_expires_and_cancels_status() -> None:
    notifications = CaptureNotifications()
    lease = TypingLeaseService(
        notifications=notifications,  # type: ignore[arg-type]
        timeout_seconds=0.02,
    )

    await lease.start()
    assert lease.active is True
    await asyncio.sleep(0.05)

    assert lease.active is False
    assert [type(payload) for payload in notifications.payloads] == [
        TypingStartPayload,
        TypingCancelPayload,
    ]
    await lease.close()


async def test_repeated_typing_start_renews_lease_and_explicit_stop_is_single_cancel() -> None:
    notifications = CaptureNotifications()
    lease = TypingLeaseService(
        notifications=notifications,  # type: ignore[arg-type]
        timeout_seconds=0.04,
    )

    first = await lease.start()
    await asyncio.sleep(0.025)
    renewed = await lease.start()
    await asyncio.sleep(0.025)
    assert lease.active is True
    await lease.stop()
    await asyncio.sleep(0.05)

    assert renewed.id == first.id
    assert lease.active is False
    assert [type(payload) for payload in notifications.payloads] == [
        TypingStartPayload,
        TypingCancelPayload,
    ]
    await lease.close()
