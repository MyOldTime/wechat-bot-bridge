"""外部 MCP 契约测试，生命周期受控且不访问微信网络。"""

from __future__ import annotations

import asyncio
import base64
import json
import secrets
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi import FastAPI
from mcp import ClientSession, types
from mcp.client.streamable_http import streamable_http_client

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
            base_url="http://127.0.0.1",
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


async def _authorization(app: FastAPI) -> dict[str, str]:
    snapshot = await app.state.public_api_credentials.current()
    return {"Authorization": f"Bearer {snapshot.token}"}


@asynccontextmanager
async def _session(client: httpx.AsyncClient, app: FastAPI) -> AsyncIterator[ClientSession]:
    client.headers.update(await _authorization(app))
    async with streamable_http_client("http://127.0.0.1/mcp", http_client=client) as (
        read,
        write,
        _,
    ):
        async with ClientSession(read, write) as session:
            result = await session.initialize()
            assert result.serverInfo.name == "wechat-bot-bridge"
            yield session


async def _send(session: ClientSession, message: dict[str, Any]) -> types.CallToolResult:
    return await session.call_tool("send_wechat_message", {"message": message})


def _error_code(result: types.CallToolResult) -> str:
    assert result.isError
    block = result.content[0]
    assert isinstance(block, types.TextContent)
    return json.loads(block.text)["code"]


async def test_mcp_discovery_only_exposes_the_send_tool(tmp_path: Path) -> None:
    async with (
        _running_client(tmp_path) as (client, app, _gateway),
        _session(client, app) as session,
    ):
        assert app.openapi()["paths"] == {}
        tools = (await session.list_tools()).tools
        assert [tool.name for tool in tools] == ["send_wechat_message"]
        schema = tools[0].inputSchema
        assert schema["type"] == "object"
        assert schema["additionalProperties"] is False
        message_ref = schema["properties"]["message"]["$ref"].rsplit("/", 1)[1]
        discriminator = schema["$defs"][message_ref]["discriminator"]
        assert discriminator["propertyName"] == "type"
        assert set(discriminator["mapping"]) == {
            "text",
            "image",
            "file",
            "typing_start",
            "typing_cancel",
        }
        assert _error_code(await session.call_tool("list_contacts", {})) == "unknown_tool"


async def test_discovered_fields_explain_variants_limits_and_markdown(tmp_path: Path) -> None:
    async with (
        _running_client(tmp_path, message_max_chars=300, media_max_bytes=8192) as (
            client,
            app,
            gateway,
        ),
        _session(client, app) as session,
    ):
        (tool,) = (await session.list_tools()).tools
        assert tool.description is not None
        assert "300 字符" in tool.description
        assert "8192 字节" in tool.description
        schema = tool.inputSchema
        message = schema["properties"]["message"]
        assert message["description"]
        variants = schema["$defs"][message["$ref"].rsplit("/", 1)[1]]["discriminator"]["mapping"]
        for kind, ref in variants.items():
            fields = schema["$defs"][ref.rsplit("/", 1)[1]]["properties"]
            assert fields["type"]["const"] == kind
            assert all(field.get("description") for field in fields.values()), kind

        assert tool.outputSchema is not None
        output = tool.outputSchema["properties"]
        assert set(output) == {"id", "msg_id", "type", "status", "created_at", "sent_at"}
        assert all(field.get("description") for field in output.values())

        text = schema["$defs"][variants["text"].rsplit("/", 1)[1]]["properties"]["text"]
        assert "Markdown" in text["description"]
        example = text["examples"][0]
        assert "\n- " in example
        await _seed_owner(app)
        result = await _send(session, {"type": "text", "text": example})
        assert not result.isError
        assert gateway.sent[0][2] == TextPayload(text=example)


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
        "wechat_bot_bridge.application.configuration.secrets.compare_digest",
        recording_compare_digest,
    )
    body = {"jsonrpc": "2.0", "id": 1, "method": "tools/list"}
    async with _running_client(tmp_path) as (client, app, _gateway):
        authorization = await _authorization(app)
        expected_token = (await app.state.public_api_credentials.current()).token
        missing = await client.post("/mcp", json=body)
        wrong = await client.post(
            "/mcp",
            headers={"Authorization": "Bearer definitely-wrong"},
            json=body,
        )
        authorization["Accept"] = "application/json, text/event-stream"
        correct = await client.post("/mcp", headers=authorization, json=body)

        assert missing.status_code == 401
        assert missing.headers["www-authenticate"] == "Bearer"
        assert missing.json()["detail"]["code"] == "unauthorized"
        assert wrong.status_code == 401
        assert correct.status_code == 200
        assert calls == [
            ("", expected_token),
            ("definitely-wrong", expected_token),
            (expected_token, expected_token),
        ]


async def test_each_call_is_a_distinct_synchronous_message(tmp_path: Path) -> None:
    async with (
        _running_client(tmp_path) as (client, app, gateway),
        _session(client, app) as session,
    ):
        await _seed_owner(app)
        request = {"type": "text", "text": "task complete"}
        first, second = await asyncio.gather(_send(session, request), _send(session, request))
        assert not first.isError and not second.isError
        results = [first.structuredContent, second.structuredContent]
        assert all(result and result["status"] == "sent" for result in results)
        assert {result["msg_id"] for result in results} == {"server-message-1", "server-message-2"}
        assert {result["id"] for result in results} == {call[0] for call in gateway.sent}
        assert results[0]["id"] != results[1]["id"]
        assert all(
            call[1:] == ("user-1", TextPayload(text="task complete"), "private-context")
            for call in gateway.sent
        )


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

    async with (
        _running_client(tmp_path) as (client, app, gateway),
        _session(client, app) as session,
    ):
        await _seed_owner(app)
        responses = [await _send(session, body) for body, _expected_type in requests]

        assert not any(response.isError for response in responses)
        assert [response.structuredContent["type"] for response in responses] == [
            expected_type for _body, expected_type in requests
        ]
        assert [response.structuredContent["msg_id"] for response in responses] == [
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
    async with (
        _running_client(tmp_path) as (client, app, gateway),
        _session(client, app) as session,
    ):
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
            {"type": "text", "text": "test", "user_id": "another-owner"},
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
        responses = [await _send(session, body) for body in bodies]
        assert all(_error_code(response) == "invalid_message" for response in responses)
        assert "private-chat-message" not in str(responses)
        assert gateway.sent == []


async def test_mcp_accepts_media_larger_than_the_sdk_default_limit(tmp_path: Path) -> None:
    # 3 MiB 的媒体编码后仅信封就会使请求突破 SDK 默认的 4 MiB 上限。
    data = b"x" * (3 * 1024 * 1024)
    async with (
        _running_client(tmp_path) as (client, app, gateway),
        _session(client, app) as session,
    ):
        await _seed_owner(app)
        result = await _send(
            session, {"type": "image", "content_base64": base64.b64encode(data).decode("ascii")}
        )
        assert not result.isError
        assert gateway.sent[0][2] == ImagePayload(data=data)


async def test_external_endpoint_maps_application_errors(tmp_path: Path) -> None:
    async with (
        _running_client(tmp_path, message_max_chars=5, media_max_bytes=1) as (client, app, gateway),
        _session(client, app) as session,
    ):

        async def send_text(text: str) -> types.CallToolResult:
            return await _send(session, {"type": "text", "text": text})

        assert _error_code(await send_text("   ")) == "invalid_message"
        gateway._session_fingerprint = None
        assert _error_code(await send_text("hello")) == "wechat_not_ready"
        gateway._session_fingerprint = SESSION
        await app.state.store.activate_session(SESSION)
        assert _error_code(await send_text("hello")) == "owner_not_bound"
        await _seed_owner(app, context_token="")
        assert _error_code(await send_text("hello")) == "interaction_required"
        await _seed_owner(app)
        assert _error_code(await send_text("123456")) == "invalid_message"
        assert (
            _error_code(await _send(session, {"type": "image", "content_base64": "YWI="}))
            == "invalid_message"
        )
        gateway.outcomes.append(GatewayTemporaryError("temporary upstream failure"))
        assert _error_code(await send_text("hello")) == "wechat_temporary_error"


async def test_mcp_transport_checks_origin_and_authenticates_other_methods(tmp_path: Path) -> None:
    async with _running_client(tmp_path) as (client, app, gateway):
        headers = await _authorization(app)
        for method in ("GET", "DELETE", "PUT"):
            assert (await client.request(method, "/mcp")).status_code == 401
        rejected = await client.post(
            "/mcp",
            headers={**headers, "Origin": "https://untrusted.invalid"},
            json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
        )
        assert rejected.status_code == 403
        assert not gateway.sent


async def test_invalid_protocol_envelope_does_not_echo_input(tmp_path: Path) -> None:
    async with _running_client(tmp_path) as (client, app, _gateway):
        headers = {**await _authorization(app), "Accept": "application/json, text/event-stream"}
        invalid = await client.post("/mcp", headers=headers, json={"text": "private-notification"})
        assert invalid.status_code == 400
        assert invalid.json()["error"]["code"] == -32602
        assert "private-notification" not in invalid.text


async def test_mcp_checks_the_configured_public_host_and_origin(tmp_path: Path) -> None:
    origin = "https://bridge.example.test:8443"
    async with _running_client(tmp_path, mcp_public_origin=origin) as (client, app, _gateway):
        headers = {**await _authorization(app), "Accept": "application/json, text/event-stream"}
        rpc = {"jsonrpc": "2.0", "id": 1, "method": "tools/list"}
        ok = await client.post(f"{origin}/mcp", headers={**headers, "Origin": origin}, json=rpc)
        assert ok.json()["result"]["tools"][0]["name"] == "send_wechat_message"
        untrusted = await client.post("https://other.example.test/mcp", headers=headers, json=rpc)
        assert untrusted.status_code == 421
        foreign = await client.post(
            f"{origin}/mcp", headers={**headers, "Origin": "https://other.example.test"}, json=rpc
        )
        assert foreign.status_code == 403


async def test_removed_external_routes_are_not_available(tmp_path: Path) -> None:
    async with _running_client(tmp_path) as (client, app, _gateway):
        authorization = await _authorization(app)
        message_list = await client.get("/api/v1/messages", headers=authorization)
        status_route = await client.get("/api/v1/wechat/status", headers=authorization)
        recipients = await client.get("/api/v1/recipients", headers=authorization)
        message_lookup = await client.get(
            "/api/v1/messages/unknown",
            headers=authorization,
        )

        assert (
            await client.post("/api/v1/messages", headers=authorization, json={})
        ).status_code == 404
        assert message_list.status_code == 404
        assert status_route.status_code == 404
        assert recipients.status_code == 404
        assert message_lookup.status_code == 404
