"""同源管理页面、Basic Auth、聊天内容和 WebSocket 契约测试。"""

from __future__ import annotations

import asyncio
import base64
import re
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import timedelta
from functools import partial
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from wechat_bot_bridge.bootstrap.app import create_app
from wechat_bot_bridge.core.config import Settings
from wechat_bot_bridge.domain.models import (
    GatewayPhase,
    GatewayStatus,
    InboundEvent,
    InboundMedia,
    InboundMessageBody,
    InboundQuote,
    InboundSender,
    OutboundPayload,
    TypingCancelPayload,
    TypingStartPayload,
    WeChatDeliveryReceipt,
)
from wechat_bot_bridge.infrastructure.inbound.dispatcher import InboundDispatcher
from wechat_bot_bridge.infrastructure.persistence.sqlite import SQLiteStore, utc_now
from wechat_bot_bridge.infrastructure.realtime.conversation import ResetBroadcast

VALID_API_KEY = "unit-test-api-key-123456"
WEB_USERNAME = "manager"
WEB_PASSWORD = "web-test-password-123"
SESSION = "session-a"
BEARER = {"Authorization": f"Bearer {VALID_API_KEY}"}
WEB_BASIC = {
    "Authorization": "Basic "
    + base64.b64encode(f"{WEB_USERNAME}:{WEB_PASSWORD}".encode()).decode("ascii")
}


class FakeGateway:
    def __init__(self, credentials_path: Path, *, phase: GatewayPhase) -> None:
        self._stop = asyncio.Event()
        self._credentials_path = credentials_path
        self._phase = phase
        self._session_fingerprint = SESSION if phase is GatewayPhase.LISTENING else None
        self._login_qr_url: str | None = None
        self.sent: list[OutboundPayload] = []
        self.start_login_calls = 0
        self.logout_calls = 0
        self.verification_codes: list[str] = []

    @property
    def status(self) -> GatewayStatus:
        return GatewayStatus(
            phase=self._phase,
            authenticated=self._phase is GatewayPhase.LISTENING,
            listener_running=self._phase is GatewayPhase.LISTENING,
            qr_ready=self._login_qr_url is not None,
            verification_required=self._phase is GatewayPhase.LOGGING_IN
            and self._login_qr_url is None,
        )

    @property
    def session_fingerprint(self) -> str | None:
        return self._session_fingerprint

    @property
    def login_qr_url(self) -> str | None:
        return self._login_qr_url

    async def run(self) -> None:
        await self._stop.wait()

    async def stop(self) -> None:
        self._stop.set()

    async def start_login(self) -> None:
        self.start_login_calls += 1
        self._phase = GatewayPhase.LOGGING_IN
        self._login_qr_url = "https://weixin.qq.com/x/private-unit-qr"

    async def submit_verification_code(self, code: str) -> None:
        self.verification_codes.append(code)

    async def logout(self) -> None:
        self.logout_calls += 1
        self._credentials_path.unlink(missing_ok=True)
        self._session_fingerprint = None
        self._login_qr_url = None
        self._phase = GatewayPhase.LOGIN_REQUIRED

    async def send_message(
        self,
        *,
        message_id: str,
        user_id: str,
        payload: OutboundPayload,
        context_token: str,
    ) -> WeChatDeliveryReceipt:
        assert user_id == "owner-user"
        assert context_token == "private-context"
        self.sent.append(payload)
        return WeChatDeliveryReceipt(
            wechat_message_id="server-item-reference",
            protocol_item_id="server-item-reference",
        )


def _settings(tmp_path: Path, **overrides: Any) -> Settings:
    values: dict[str, Any] = {
        "api_key": VALID_API_KEY,
        "web_username": WEB_USERNAME,
        "web_password": WEB_PASSWORD,
        "data_dir": tmp_path,
        "database_path": tmp_path / "bridge.db",
        "credentials_path": tmp_path / "wechat" / "credentials.json",
        "_env_file": None,
    }
    values.update(overrides)
    return Settings(**values)


def _app(
    tmp_path: Path,
    *,
    gateway_phase: GatewayPhase = GatewayPhase.LISTENING,
    **overrides: Any,
) -> tuple[FastAPI, FakeGateway]:
    settings = _settings(tmp_path, **overrides)
    gateway = FakeGateway(settings.credentials_path, phase=gateway_phase)

    def factory(
        _settings: Settings,
        _store: SQLiteStore,
        _dispatcher: InboundDispatcher,
    ) -> FakeGateway:
        return gateway

    return (
        create_app(settings, gateway_factory=factory),
        gateway,
    )


@asynccontextmanager
async def _running_client(
    tmp_path: Path,
    *,
    gateway_phase: GatewayPhase = GatewayPhase.LISTENING,
    **overrides: Any,
) -> AsyncIterator[tuple[httpx.AsyncClient, FastAPI, FakeGateway]]:
    app, gateway = _app(tmp_path, gateway_phase=gateway_phase, **overrides)
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(
            transport=transport,
            base_url="http://testserver",
        ) as client:
            yield client, app, gateway


async def _seed_owner(app: FastAPI) -> None:
    await app.state.store.activate_session(SESSION)
    await app.state.store.save_owner_context(
        user_id="owner-user",
        context_token="private-context",
        credential_fingerprint=SESSION,
    )


def _extract_admin_token(html: str) -> str:
    match = re.search(r'<meta name="admin-token" content="([^"]+)">', html)
    assert match is not None
    return match.group(1)


async def test_management_page_is_disabled_until_password_is_configured(
    tmp_path: Path,
) -> None:
    async with _running_client(tmp_path, web_password=None) as (client, _app, _gateway):
        response = await client.get("/admin")

        assert response.status_code == 503
        assert response.json()["detail"]["code"] == "web_admin_disabled"


async def test_management_basic_auth_is_separate_from_external_bearer(
    tmp_path: Path,
) -> None:
    async with _running_client(tmp_path) as (client, app, _gateway):
        await _seed_owner(app)
        missing = await client.get("/admin")
        bearer_on_web = await client.get("/admin", headers=BEARER)
        page = await client.get("/admin", headers=WEB_BASIC)
        chat_script = await client.get("/admin/static/chat.js", headers=WEB_BASIC)
        chat_style = await client.get("/admin/static/chat.css", headers=WEB_BASIC)
        basic_on_external = await client.post(
            "/api/v1/messages",
            headers=WEB_BASIC,
            json={"type": "text", "text": "wrong authentication"},
        )
        bearer_on_external = await client.post(
            "/api/v1/messages",
            headers=BEARER,
            json={"type": "text", "text": "correct authentication"},
        )

        assert missing.status_code == 401
        assert missing.headers["www-authenticate"].startswith("Basic")
        assert bearer_on_web.status_code == 401
        assert page.status_code == 200
        assert "/admin/static/chat.css" in page.text
        assert "/admin/static/chat.js" in page.text
        assert 'id="login-panel"' in page.text
        assert 'action="/admin/logout"' in page.text
        assert '<meta name="typing-timeout-seconds" content="5">' in page.text
        assert "桥接引用" not in page.text
        assert "quote-message-action" not in page.text
        assert chat_script.status_code == 200
        assert "微信原生引用" in chat_script.text
        assert "focusQuotedMessage" in chat_script.text
        assert "quote-target" in chat_script.text
        assert "quote_message_id" not in chat_script.text
        assert 'addEventListener("contextmenu"' not in chat_script.text
        assert chat_style.status_code == 200
        assert "animation: quote-highlight 0.5s ease-in-out 2" in chat_style.text
        assert page.headers["cache-control"] == "no-store"
        assert "frame-ancestors 'none'" in page.headers["content-security-policy"]
        assert basic_on_external.status_code == 401
        assert bearer_on_external.status_code == 200


async def test_admin_starts_login_and_serves_qr_without_exposing_raw_url(
    tmp_path: Path,
) -> None:
    async with _running_client(
        tmp_path,
        gateway_phase=GatewayPhase.LOGIN_REQUIRED,
    ) as (client, _app, gateway):
        page = await client.get("/admin", headers=WEB_BASIC)
        token = _extract_admin_token(page.text)

        unauthorized_start = await client.post(
            "/admin/api/login",
            headers={"X-Admin-Token": token},
        )
        invalid_token = await client.post(
            "/admin/api/login",
            headers={**WEB_BASIC, "X-Admin-Token": "invalid"},
        )
        started = await client.post(
            "/admin/api/login",
            headers={**WEB_BASIC, "X-Admin-Token": token},
        )
        unauthorized_qr = await client.get("/admin/api/login/qr")
        qr = await client.get("/admin/api/login/qr", headers=WEB_BASIC)

        assert unauthorized_start.status_code == 401
        assert invalid_token.status_code == 403
        assert started.status_code == 200
        assert started.json()["phase"] == "logging_in"
        assert started.json()["qr_ready"] is True
        assert gateway.start_login_calls == 1
        assert unauthorized_qr.status_code == 401
        assert qr.status_code == 200
        assert qr.headers["content-type"].startswith("image/svg+xml")
        assert qr.headers["cache-control"] == "no-store"
        assert b"private-unit-qr" not in qr.content

        gateway._login_qr_url = None
        verified = await client.post(
            "/admin/api/login/verification",
            headers={**WEB_BASIC, "X-Admin-Token": token},
            json={"code": "654321"},
        )
        assert verified.status_code == 200
        assert gateway.verification_codes == ["654321"]


async def test_admin_logout_form_clears_all_state_and_restarts_login(
    tmp_path: Path,
) -> None:
    inbound = InboundEvent(
        event_id="event-cleared-by-logout",
        occurred_at=utc_now(),
        sender=InboundSender(user_id="owner-user"),
        message=InboundMessageBody(type="text", text="退出后不应存在"),
    )
    async with _running_client(tmp_path) as (client, app, gateway):
        await _seed_owner(app)
        await app.state.store.set_state("cursor:session-a", "private-cursor")
        app.state.settings.credentials_path.write_text(
            '{"token":"fake-test-token"}',
            encoding="utf-8",
        )
        await app.state.dispatcher.publish(inbound)
        await app.state.dispatcher.drain()
        sent = await client.post(
            "/api/v1/messages",
            headers=BEARER,
            json={"type": "text", "text": "也需要被清除"},
        )
        assert sent.status_code == 200

        page = await client.get("/admin", headers=WEB_BASIC)
        token = _extract_admin_token(page.text)
        unauthorized = await client.post(
            "/admin/logout",
            data={"admin_token": token},
        )
        invalid = await client.post(
            "/admin/logout",
            headers=WEB_BASIC,
            data={"admin_token": "invalid"},
        )

        async with app.state.conversation_hub.subscribe() as queue:
            logged_out = await client.post(
                "/admin/logout",
                headers=WEB_BASIC,
                data={"admin_token": token},
            )
            reset = await queue.get()

        assert unauthorized.status_code == 401
        assert invalid.status_code == 403
        assert logged_out.status_code == 303
        assert logged_out.headers["location"] == "/admin"
        assert isinstance(reset, ResetBroadcast)
        assert gateway.logout_calls == 1
        assert gateway.start_login_calls == 1
        assert not app.state.settings.credentials_path.exists()
        assert await app.state.store.get_active_fingerprint() is None
        assert await app.state.store.get_state("cursor:session-a") is None
        assert await app.state.store.resolve_owner(SESSION) is None
        history = await client.get("/admin/api/messages", headers=WEB_BASIC)
        assert history.json()["items"] == []
        assert await app.state.store.remember_inbound_event(inbound) is True
        status = await client.get("/admin/api/status", headers=WEB_BASIC)
        assert status.json()["phase"] == "logging_in"
        assert status.json()["qr_ready"] is True


async def test_chat_api_shows_full_content_and_media_only_with_basic_auth(
    tmp_path: Path,
) -> None:
    png = b"\x89PNG\r\n\x1a\nimage-payload"
    encoded = base64.b64encode(png).decode("ascii")
    async with _running_client(tmp_path) as (client, app, _gateway):
        await _seed_owner(app)
        text_response = await client.post(
            "/api/v1/messages",
            headers=BEARER,
            json={"type": "text", "text": "自动化任务已完成"},
        )
        image_response = await client.post(
            "/api/v1/messages",
            headers=BEARER,
            json={
                "type": "image",
                "content_base64": encoded,
                "caption": "任务截图",
            },
        )
        unauthorized = await client.get("/admin/api/messages")
        page = await client.get("/admin/api/messages", headers=WEB_BASIC)

        assert text_response.status_code == 200
        assert image_response.status_code == 200
        assert unauthorized.status_code == 401
        assert page.status_code == 200
        assert [item["text"] for item in page.json()["items"]] == [
            "自动化任务已完成",
            "任务截图",
        ]
        assert all(item["origin"] == "external_api" for item in page.json()["items"])

        image = page.json()["items"][1]
        media_unauthorized = await client.get(image["media_url"])
        media = await client.get(image["media_url"], headers=WEB_BASIC)
        assert media_unauthorized.status_code == 401
        assert media.status_code == 200
        assert media.content == png
        assert media.headers["content-type"] == "image/png"
        assert media.headers["x-content-type-options"] == "nosniff"


async def test_admin_send_requires_page_token_and_records_web_origin(
    tmp_path: Path,
) -> None:
    async with _running_client(tmp_path) as (client, app, gateway):
        await _seed_owner(app)
        page = await client.get("/admin", headers=WEB_BASIC)
        token = _extract_admin_token(page.text)

        missing_token = await client.post(
            "/admin/api/messages",
            headers=WEB_BASIC,
            json={"type": "text", "text": "missing token"},
        )
        invalid_token = await client.post(
            "/admin/api/messages",
            headers={**WEB_BASIC, "X-Admin-Token": "invalid"},
            json={"type": "text", "text": "invalid token"},
        )
        sent = await client.post(
            "/admin/api/messages",
            headers={**WEB_BASIC, "X-Admin-Token": token},
            json={"type": "text", "text": "从页面发送"},
        )
        history = await client.get("/admin/api/messages", headers=WEB_BASIC)

        assert missing_token.status_code == 422
        assert invalid_token.status_code == 403
        assert sent.status_code == 200
        assert "msg_id" not in sent.json()
        assert len(gateway.sent) == 1
        assert history.json()["items"][0]["text"] == "从页面发送"
        assert history.json()["items"][0]["origin"] == "web_admin"


async def test_admin_typing_requests_use_server_lease_and_explicit_cancel(
    tmp_path: Path,
) -> None:
    async with _running_client(tmp_path) as (client, app, gateway):
        await _seed_owner(app)
        page = await client.get("/admin", headers=WEB_BASIC)
        token = _extract_admin_token(page.text)
        headers = {**WEB_BASIC, "X-Admin-Token": token}

        started = await client.post(
            "/admin/api/messages",
            headers=headers,
            json={"type": "typing_start"},
        )
        stopped = await client.post(
            "/admin/api/messages",
            headers=headers,
            json={"type": "typing_cancel"},
        )

        assert started.status_code == 200
        assert stopped.status_code == 200
        assert [type(payload) for payload in gateway.sent] == [
            TypingStartPayload,
            TypingCancelPayload,
        ]


async def test_admin_rejects_removed_outbound_quote_field(
    tmp_path: Path,
) -> None:
    async with _running_client(tmp_path) as (client, app, gateway):
        await _seed_owner(app)
        page = await client.get("/admin", headers=WEB_BASIC)
        token = _extract_admin_token(page.text)

        rejected = await client.post(
            "/admin/api/messages",
            headers={**WEB_BASIC, "X-Admin-Token": token},
            json={
                "type": "text",
                "text": "引用回复",
                "quote_message_id": "removed-feature",
            },
        )

        assert rejected.status_code == 422
        assert gateway.sent == []


async def test_inbound_dispatcher_reuses_chat_history_sink_and_updates_history(
    tmp_path: Path,
) -> None:
    original_time = utc_now()
    original = InboundEvent(
        event_id="wechat-event-original",
        occurred_at=original_time,
        sender=InboundSender(user_id="owner-user"),
        message=InboundMessageBody(type="text", text="测试"),
        protocol_item_id="wechat-item-original",
    )
    reply = InboundEvent(
        event_id="wechat-event-1",
        occurred_at=original_time + timedelta(seconds=1),
        sender=InboundSender(user_id="owner-user"),
        message=InboundMessageBody(type="text", text="微信刚发来的内容"),
        protocol_item_id="wechat-item-reply",
        quote=InboundQuote(
            title="微信桥接",
            text="",
            protocol_item_id="wechat-item-original",
        ),
    )
    async with _running_client(tmp_path) as (client, app, _gateway):
        await app.state.dispatcher.publish(original)
        await app.state.dispatcher.publish(reply)

        for _attempt in range(50):
            history = await client.get("/admin/api/messages", headers=WEB_BASIC)
            if len(history.json()["items"]) == 2:
                break
            await asyncio.sleep(0.01)

        item = history.json()["items"][1]
        assert item["direction"] == "inbound"
        assert item["origin"] == "wechat"
        assert item["text"] == "微信刚发来的内容"
        assert item["quote"] == {
            "message_id": "wechat-event-original",
            "type": "text",
            "text": "测试",
            "title": "微信桥接",
        }
        assert "owner-user" not in history.text


async def test_wechat_quote_of_admin_outbound_recovers_clickable_original(
    tmp_path: Path,
) -> None:
    """模拟微信引用网页发出的消息，并校验历史接口返回原文定位 ID。"""

    async with _running_client(tmp_path) as (client, app, _gateway):
        await _seed_owner(app)
        page = await client.get("/admin", headers=WEB_BASIC)
        token = _extract_admin_token(page.text)
        sent = await client.post(
            "/admin/api/messages",
            headers={**WEB_BASIC, "X-Admin-Token": token},
            json={"type": "text", "text": "你好"},
        )
        sent_id = sent.json()["id"]
        server_reference = "server-item-reference"
        await app.state.dispatcher.publish(
            InboundEvent(
                event_id="wechat-quotes-admin-message",
                occurred_at=utc_now() + timedelta(seconds=1),
                sender=InboundSender(user_id="owner-user"),
                message=InboundMessageBody(type="text", text="你好"),
                quote=InboundQuote(
                    title="微信原生引用",
                    text="",
                    protocol_item_id=server_reference,
                ),
            )
        )

        for _attempt in range(50):
            history = await client.get("/admin/api/messages", headers=WEB_BASIC)
            if len(history.json()["items"]) == 2:
                break
            await asyncio.sleep(0.01)

        quoted_reply = history.json()["items"][1]
        assert quoted_reply["quote"] == {
            "message_id": sent_id,
            "type": "text",
            "text": "你好",
            "title": "微信原生引用",
        }


async def test_inbound_downloaded_media_is_exposed_only_by_protected_link(
    tmp_path: Path,
) -> None:
    png = b"\x89PNG\r\n\x1a\nwechat-inbound-image"
    event = InboundEvent(
        event_id="wechat-image-1",
        occurred_at=utc_now(),
        sender=InboundSender(user_id="owner-user"),
        message=InboundMessageBody(type="image", text="[image]"),
        media=InboundMedia(data=png, mime_type="image/png"),
    )
    async with _running_client(tmp_path) as (client, app, _gateway):
        await app.state.dispatcher.publish(event)

        for _attempt in range(50):
            history = await client.get("/admin/api/messages", headers=WEB_BASIC)
            if history.json()["items"]:
                break
            await asyncio.sleep(0.01)

        item = history.json()["items"][0]
        assert item["type"] == "image"
        assert item["media_url"] == "/admin/api/messages/wechat-image-1/media"
        unauthorized = await client.get(item["media_url"])
        media = await client.get(item["media_url"], headers=WEB_BASIC)
        assert unauthorized.status_code == 401
        assert media.status_code == 200
        assert media.headers["content-type"] == "image/png"
        assert media.content == png


async def test_admin_history_rejects_invalid_cursor_and_exposes_status(
    tmp_path: Path,
) -> None:
    async with _running_client(tmp_path) as (client, _app, _gateway):
        invalid = await client.get(
            "/admin/api/messages",
            headers=WEB_BASIC,
            params={"cursor": "invalid"},
        )
        status = await client.get("/admin/api/status", headers=WEB_BASIC)

        assert invalid.status_code == 422
        assert invalid.json()["detail"]["code"] == "invalid_cursor"
        assert status.status_code == 200
        assert status.json()["phase"] == "listening"


def test_websocket_pushes_external_message_status_changes(tmp_path: Path) -> None:
    app, _gateway = _app(tmp_path)
    with TestClient(app) as client:
        assert client.portal is not None
        client.portal.call(app.state.store.activate_session, SESSION)
        client.portal.call(
            partial(
                app.state.store.save_owner_context,
                user_id="owner-user",
                context_token="private-context",
                credential_fingerprint=SESSION,
            )
        )
        page = client.get("/admin", headers=WEB_BASIC)
        token = _extract_admin_token(page.text)

        with client.websocket_connect(
            "/admin/ws",
            subprotocols=["wbb-admin", f"wbb-token.{token}"],
        ) as websocket:
            connected = websocket.receive_json()
            response = client.post(
                "/api/v1/messages",
                headers=BEARER,
                json={"type": "text", "text": "实时展示这条消息"},
            )
            sending = websocket.receive_json()
            sent = websocket.receive_json()

        assert connected["type"] == "connected"
        assert response.status_code == 200
        assert sending["type"] == "message_upsert"
        assert sending["message"]["status"] == "sending"
        assert sending["message"]["text"] == "实时展示这条消息"
        assert sent["message"]["status"] == "sent"
        assert sent["message"]["id"] == sending["message"]["id"]


def test_websocket_rejects_invalid_management_token(tmp_path: Path) -> None:
    app, _gateway = _app(tmp_path)
    with TestClient(app) as client:
        with pytest.raises(WebSocketDisconnect) as raised:
            with client.websocket_connect(
                "/admin/ws",
                subprotocols=["wbb-admin", "wbb-token.invalid"],
            ):
                pass
        assert raised.value.code == 4401
