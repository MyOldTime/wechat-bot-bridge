"""入站分发队列排空语义测试。"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
from datetime import UTC, datetime

import httpx
import pytest

from wechat_bot_bridge.domain.models import (
    ForwardingScheme,
    InboundEvent,
    InboundMessageBody,
    InboundSender,
    OutboundForwardingConfig,
)
from wechat_bot_bridge.infrastructure.inbound.dispatcher import (
    InboundDispatcher,
    OutboundForwardingSink,
)


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


class MutableForwardingConfig:
    def __init__(self, config: OutboundForwardingConfig) -> None:
        self.config = config

    async def current(self) -> OutboundForwardingConfig:
        return self.config


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


async def test_forwarding_sink_uses_latest_config_and_signs_raw_body() -> None:
    secret = "unit-test-forwarding-secret-123456789"
    now = datetime(2026, 8, 3, 12, 0, tzinfo=UTC)
    provider = MutableForwardingConfig(OutboundForwardingConfig(secret=secret, updated_at=now))
    requests: list[httpx.Request] = []

    async def handle(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(202, request=request)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handle), follow_redirects=False)
    sink = OutboundForwardingSink(config_provider=provider, client=client)
    event = InboundEvent(
        event_id="event-to-forward",
        occurred_at=now,
        sender=InboundSender(user_id="owner"),
        message=InboundMessageBody(type="text", text="执行测试"),
    )

    try:
        await sink.deliver(event)
        assert requests == []

        provider.config = provider.config.model_copy(
            update={
                "enabled": True,
                "scheme": ForwardingScheme.HTTP,
                "host": "127.0.0.1",
                "port": 8765,
                "path": "/v1/sources/wechat-bridge/events",
            }
        )
        await sink.deliver(event)
    finally:
        await sink.close()
        await client.aclose()

    assert len(requests) == 1
    request = requests[0]
    body = request.content
    timestamp = request.headers["X-WeChat-Bridge-Timestamp"]
    expected_signature = hmac.new(
        secret.encode(),
        timestamp.encode("ascii") + b"." + body,
        hashlib.sha256,
    ).hexdigest()
    assert str(request.url) == "http://127.0.0.1:8765/v1/sources/wechat-bridge/events"
    assert request.headers["X-WeChat-Bridge-Event"] == event.event_id
    assert request.headers["X-WeChat-Bridge-Signature"] == f"sha256={expected_signature}"
    assert json.loads(body) == json.loads(event.model_dump_json())


async def test_forwarding_sink_retries_with_configured_bound(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = OutboundForwardingConfig(
        enabled=True,
        secret="unit-test-forwarding-secret-123456789",
        max_attempts=2,
        updated_at=datetime(2026, 8, 3, 12, 0, tzinfo=UTC),
    )
    provider = MutableForwardingConfig(config)
    attempts = 0

    async def handle(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        return httpx.Response(503 if attempts == 1 else 202, request=request)

    async def no_sleep(_seconds: float) -> None:
        return None

    # 避免测试真实等待退避时间。
    monkeypatch.setattr(
        "wechat_bot_bridge.infrastructure.inbound.dispatcher.asyncio.sleep",
        no_sleep,
    )
    client = httpx.AsyncClient(transport=httpx.MockTransport(handle))
    sink = OutboundForwardingSink(config_provider=provider, client=client)
    event = InboundEvent(
        event_id="retry-event",
        occurred_at=datetime(2026, 8, 3, 12, 0, tzinfo=UTC),
        sender=InboundSender(user_id="owner"),
        message=InboundMessageBody(type="text", text="重试一次"),
    )
    try:
        await sink.deliver(event)
    finally:
        await client.aclose()

    assert attempts == 2
