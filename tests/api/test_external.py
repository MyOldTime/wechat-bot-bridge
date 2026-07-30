"""外部 FastAPI 契约测试，生命周期受控且不访问微信网络。"""

from __future__ import annotations

import asyncio
import base64
import secrets
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi import FastAPI

from wechat_bot_bridge.bootstrap.app import create_app
from wechat_bot_bridge.core.config import Settings
from wechat_bot_bridge.domain.errors import GatewayTemporaryError
from wechat_bot_bridge.domain.models import (
    FilePayload,
    GatewayPhase,
    GatewayStatus,
    ImagePayload,
    OutboundPayload,
    TextPayload,
    TypingCancelPayload,
    TypingStartPayload,
    WeChatDeliveryReceipt,
)
from wechat_bot_bridge.infrastructure.inbound.dispatcher import InboundDispatcher
from wechat_bot_bridge.infrastructure.persistence.sqlite import SQLiteStore

VALID_API_KEY = "unit-test-api-key-123456"
AUTHORIZATION = {"Authorization": f"Bearer {VALID_API_KEY}"}
SESSION = "session-a"


class FakeGateway:
    def __init__(self) -> None:
        self._stop = asyncio.Event()
        self._session_fingerprint: str | None = SESSION
        self.sent: list[tuple[str, str, OutboundPayload, str]] = []
        self.outcomes: list[Exception | None] = []
        self.stop_called = False

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
        await self._stop.wait()

    async def stop(self) -> None:
        self.stop_called = True
        self._stop.set()

    async def send_message(
        self,
        *,
        message_id: str,
        user_id: str,
        payload: OutboundPayload,
        context_token: str,
    ) -> WeChatDeliveryReceipt:
        self.sent.append((message_id, user_id, payload, context_token))
        if self.outcomes:
            outcome = self.outcomes.pop(0)
            if outcome is not None:
                raise outcome
        is_typing = isinstance(payload, (TypingStartPayload, TypingCancelPayload))
        server_message_id = None if is_typing else f"server-message-{len(self.sent)}"
        return WeChatDeliveryReceipt(
            wechat_message_id=server_message_id,
            protocol_item_id=(None if is_typing else f"server-message-hash-{len(self.sent)}"),
        )


def _settings(tmp_path: Path, **overrides: Any) -> Settings:
    values: dict[str, Any] = {
        "api_key": VALID_API_KEY,
        "data_dir": tmp_path,
        "database_path": tmp_path / "bridge.db",
        "credentials_path": tmp_path / "wechat" / "credentials.json",
        "_env_file": None,
    }
    values.update(overrides)
    return Settings(**values)


@asynccontextmanager
async def _running_client(
    tmp_path: Path,
    **setting_overrides: Any,
) -> AsyncIterator[tuple[httpx.AsyncClient, FastAPI, FakeGateway]]:
    gateway = FakeGateway()

    def factory(
        _settings: Settings,
        _store: SQLiteStore,
        _dispatcher: InboundDispatcher,
    ) -> FakeGateway:
        return gateway

    app = create_app(
        _settings(tmp_path, **setting_overrides),
        gateway_factory=factory,
    )
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(
            transport=transport,
            base_url="http://testserver",
        ) as client:
            yield client, app, gateway
    assert gateway.stop_called is True
    assert app.state.store.is_open is False


async def _seed_owner(
    app: FastAPI,
    *,
    context_token: str = "private-context",
) -> None:
    await app.state.store.activate_session(SESSION)
    await app.state.store.save_owner_context(
        user_id="user-1",
        context_token=context_token,
        credential_fingerprint=SESSION,
    )


def test_openapi_exposes_only_the_single_external_message_endpoint(
    tmp_path: Path,
) -> None:
    schema = create_app(_settings(tmp_path)).openapi()

    assert set(schema["paths"]) == {"/api/v1/messages"}
    assert set(schema["paths"]["/api/v1/messages"]) == {"post"}
    request_schema = schema["paths"]["/api/v1/messages"]["post"]["requestBody"]["content"][
        "application/json"
    ]["schema"]
    assert request_schema["discriminator"]["propertyName"] == "type"
    assert set(request_schema["discriminator"]["mapping"]) == {
        "text",
        "image",
        "file",
        "typing_start",
        "typing_cancel",
    }


async def test_health_endpoints_are_public_and_hidden_from_openapi(
    tmp_path: Path,
) -> None:
    async with _running_client(tmp_path) as (client, _app, _gateway):
        live = await client.get("/health/live")
        ready = await client.get("/health/ready")

        assert live.status_code == 200
        assert live.json() == {"status": "ok"}
        assert ready.status_code == 200
        assert ready.json() == {"status": "ready"}


async def test_external_endpoint_requires_bearer_auth_with_constant_time_compare(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[str, str]] = []
    original_compare_digest = secrets.compare_digest

    def recording_compare_digest(provided: str, expected: str) -> bool:
        calls.append((provided, expected))
        return original_compare_digest(provided, expected)

    monkeypatch.setattr(
        "wechat_bot_bridge.api.security.secrets.compare_digest",
        recording_compare_digest,
    )
    body = {"type": "text", "text": "task complete"}
    async with _running_client(tmp_path) as (client, app, _gateway):
        missing = await client.post("/api/v1/messages", json=body)
        wrong = await client.post(
            "/api/v1/messages",
            headers={"Authorization": "Bearer definitely-wrong"},
            json=body,
        )
        await _seed_owner(app)
        correct = await client.post("/api/v1/messages", headers=AUTHORIZATION, json=body)

        assert missing.status_code == 401
        assert missing.headers["www-authenticate"] == "Bearer"
        assert missing.json()["detail"]["code"] == "unauthorized"
        assert wrong.status_code == 401
        assert correct.status_code == 200
        assert calls == [
            ("", VALID_API_KEY),
            ("definitely-wrong", VALID_API_KEY),
            (VALID_API_KEY, VALID_API_KEY),
        ]


async def test_each_external_post_is_a_distinct_synchronous_message(
    tmp_path: Path,
) -> None:
    async with _running_client(tmp_path) as (client, app, gateway):
        await _seed_owner(app)
        request = {"type": "text", "text": "task complete"}
        # 旧客户端即使继续传相同请求头，每次 POST 仍代表一条新消息。
        headers = {**AUTHORIZATION, "Idempotency-Key": "legacy-task-1"}

        first = await client.post("/api/v1/messages", headers=headers, json=request)
        repeated = await client.post("/api/v1/messages", headers=headers, json=request)

        assert first.status_code == 200
        assert repeated.status_code == 200
        assert first.json()["status"] == "sent"
        assert first.json()["type"] == "text"
        assert first.json()["msg_id"] == "server-message-1"
        assert repeated.json()["msg_id"] == "server-message-2"
        assert first.json()["id"] != repeated.json()["id"]
        assert "location" not in first.headers
        assert gateway.sent == [
            (
                first.json()["id"],
                "user-1",
                TextPayload(text="task complete"),
                "private-context",
            ),
            (
                repeated.json()["id"],
                "user-1",
                TextPayload(text="task complete"),
                "private-context",
            ),
        ]


async def test_external_endpoint_supports_public_sdk_message_types(
    tmp_path: Path,
) -> None:
    encoded = base64.b64encode(b"binary-content").decode("ascii")
    requests = [
        ({"type": "text", "text": "hello"}, "text"),
        (
            {"type": "image", "content_base64": encoded, "caption": "screenshot"},
            "image",
        ),
        (
            {
                "type": "file",
                "content_base64": encoded,
                "file_name": "report.pdf",
                "caption": "report",
            },
            "file",
        ),
        ({"type": "typing_start"}, "typing_start"),
        ({"type": "typing_cancel"}, "typing_cancel"),
    ]

    async with _running_client(tmp_path) as (client, app, gateway):
        await _seed_owner(app)
        responses = [
            await client.post("/api/v1/messages", headers=AUTHORIZATION, json=body)
            for body, _expected_type in requests
        ]

        assert [response.status_code for response in responses] == [200] * 5
        assert [response.json()["type"] for response in responses] == [
            expected_type for _body, expected_type in requests
        ]
        assert [response.json()["msg_id"] for response in responses] == [
            "server-message-1",
            "server-message-2",
            "server-message-3",
            None,
            None,
        ]
        assert [call[2] for call in gateway.sent] == [
            TextPayload(text="hello"),
            ImagePayload(data=b"binary-content", caption="screenshot"),
            FilePayload(data=b"binary-content", file_name="report.pdf", caption="report"),
            TypingStartPayload(),
            TypingCancelPayload(),
        ]


async def test_invalid_and_unsupported_payloads_are_rejected(tmp_path: Path) -> None:
    async with _running_client(tmp_path) as (client, app, gateway):
        await _seed_owner(app)
        bodies = [
            {
                "type": "voice",
                "content_base64": "U0lMSw==",
                "duration_ms": 1000,
            },
            {"type": "quote", "text": "reply", "quote": {"text": "original"}},
            {"type": "image", "content_base64": "not base64!"},
            {
                "type": "file",
                "content_base64": "aW1hZ2U=",
                "file_name": "photo.png",
            },
            {"text": "legacy payload"},
            {
                "type": "text",
                "text": "legacy payload",
                "idempotency_key": "legacy-task",
            },
            {
                "type": "text",
                "text": "引用只属于管理页面",
                "quote_message_id": "private-chat-message",
            },
            {
                "type": "text",
                "text": "当前协议不支持出站原生引用",
                "quote_msg_id": "wechat-server-message-id",
            },
        ]
        responses = [
            await client.post("/api/v1/messages", headers=AUTHORIZATION, json=body)
            for body in bodies
        ]
        assert [response.status_code for response in responses] == [422] * len(bodies)
        assert gateway.sent == []


async def test_external_endpoint_maps_application_errors(tmp_path: Path) -> None:
    async with _running_client(tmp_path, message_max_chars=5) as (client, app, gateway):
        blank_message = await client.post(
            "/api/v1/messages",
            headers=AUTHORIZATION,
            json={"type": "text", "text": "   "},
        )
        gateway._session_fingerprint = None
        no_session = await client.post(
            "/api/v1/messages",
            headers=AUTHORIZATION,
            json={"type": "text", "text": "hello"},
        )
        gateway._session_fingerprint = SESSION
        await app.state.store.activate_session(SESSION)
        unbound = await client.post(
            "/api/v1/messages",
            headers=AUTHORIZATION,
            json={"type": "text", "text": "hello"},
        )
        await _seed_owner(app, context_token="")
        missing_context = await client.post(
            "/api/v1/messages",
            headers=AUTHORIZATION,
            json={"type": "text", "text": "hello"},
        )
        await _seed_owner(app)
        too_long = await client.post(
            "/api/v1/messages",
            headers=AUTHORIZATION,
            json={"type": "text", "text": "123456"},
        )
        gateway.outcomes.append(GatewayTemporaryError("temporary upstream failure"))
        upstream_failure = await client.post(
            "/api/v1/messages",
            headers=AUTHORIZATION,
            json={"type": "text", "text": "hello"},
        )

        assert blank_message.status_code == 422
        assert blank_message.json()["detail"]["code"] == "invalid_message"
        assert no_session.status_code == 503
        assert no_session.json()["detail"]["code"] == "wechat_not_ready"
        assert unbound.status_code == 409
        assert unbound.json()["detail"]["code"] == "owner_not_bound"
        assert missing_context.status_code == 409
        assert missing_context.json()["detail"]["code"] == "interaction_required"
        assert too_long.status_code == 422
        assert too_long.json()["detail"]["code"] == "invalid_message"
        assert upstream_failure.status_code == 502
        assert upstream_failure.json()["detail"]["code"] == "wechat_temporary_error"


async def test_removed_external_routes_are_not_available(tmp_path: Path) -> None:
    async with _running_client(tmp_path) as (client, _app, _gateway):
        message_list = await client.get("/api/v1/messages", headers=AUTHORIZATION)
        status_route = await client.get("/api/v1/wechat/status", headers=AUTHORIZATION)
        recipients = await client.get("/api/v1/recipients", headers=AUTHORIZATION)
        message_lookup = await client.get(
            "/api/v1/messages/unknown",
            headers=AUTHORIZATION,
        )

        assert message_list.status_code == 405
        assert status_route.status_code == 404
        assert recipients.status_code == 404
        assert message_lookup.status_code == 404
