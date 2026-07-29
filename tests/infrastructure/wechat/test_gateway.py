"""内置 iLink 客户端与领域网关之间的契约测试。"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest

from wechat_bot_bridge.core.config import Settings
from wechat_bot_bridge.domain.errors import GatewayTemporaryError
from wechat_bot_bridge.domain.models import (
    FilePayload,
    GatewayPhase,
    ImagePayload,
    InboundEvent,
    TextPayload,
    TypingCancelPayload,
    TypingStartPayload,
)
from wechat_bot_bridge.infrastructure.persistence.sqlite import SQLiteStore
from wechat_bot_bridge.infrastructure.wechat.gateway import (
    WeChatILinkGateway,
    credential_fingerprint,
)
from wechat_bot_bridge.infrastructure.wechat.ilink.auth import CredentialStore
from wechat_bot_bridge.infrastructure.wechat.ilink.errors import ILinkApiError
from wechat_bot_bridge.infrastructure.wechat.ilink.models import (
    Credentials,
    DownloadedMedia,
    IncomingMessage,
    MediaKind,
    QuotedMessage,
    UpdateBatch,
)
from wechat_bot_bridge.infrastructure.wechat.ilink.transport import ILinkTransport

VALID_API_KEY = "unit-test-api-key-123456"


class CaptureDispatcher:
    def __init__(self) -> None:
        self.events: list[InboundEvent] = []
        self.drain_calls = 0

    async def publish(self, event: InboundEvent) -> None:
        self.events.append(event)

    async def drain(self) -> None:
        self.drain_calls += 1


class FakeLoginOnlyTransport:
    def __init__(self) -> None:
        self.qr_requested = asyncio.Event()
        self.poll_started = asyncio.Event()
        self.closed = False

    async def get_qr_code(self, *, local_tokens: list[str]) -> dict[str, object]:
        assert local_tokens == []
        self.qr_requested.set()
        return {
            "qrcode": "opaque-login-id",
            "qrcode_img_content": "https://weixin.qq.com/x/private-qr",
        }

    async def poll_qr_status(
        self,
        *,
        base_url: str,
        qrcode: str,
        verify_code: str | None = None,
    ) -> dict[str, object]:
        assert base_url == "https://ilinkai.weixin.qq.com"
        assert qrcode == "opaque-login-id"
        assert verify_code is None
        self.poll_started.set()
        await asyncio.Event().wait()
        raise AssertionError("unreachable")

    async def close(self) -> None:
        self.closed = True


class FakeClient:
    def __init__(self, credentials: Credentials) -> None:
        self.credentials = credentials
        self.calls: list[tuple[object, ...]] = []
        self.downloaded: DownloadedMedia | None = None
        self.send_error: Exception | None = None

    async def get_updates(self, cursor: str) -> UpdateBatch:
        return UpdateBatch(cursor=cursor, messages=())

    async def send_text(self, **kwargs: object) -> None:
        if self.send_error is not None:
            raise self.send_error
        self.calls.append(("text", kwargs))

    async def send_image(self, **kwargs: object) -> None:
        self.calls.append(("image", kwargs))

    async def send_file(self, **kwargs: object) -> None:
        self.calls.append(("file", kwargs))

    async def set_typing(self, **kwargs: object) -> None:
        self.calls.append(("typing", kwargs))

    async def download(self, message: IncomingMessage) -> DownloadedMedia | None:
        self.calls.append(("download", message.type))
        return self.downloaded

    async def notify_start(self) -> None:
        self.calls.append(("notify_start",))

    async def notify_stop(self) -> None:
        self.calls.append(("notify_stop",))


def _settings(tmp_path: Path) -> Settings:
    return Settings(
        api_key=VALID_API_KEY,
        data_dir=tmp_path,
        database_path=tmp_path / "bridge.db",
        credentials_path=tmp_path / "wechat" / "credentials.json",
        _env_file=None,
    )


def _credentials(*, token: str = "fake-token") -> Credentials:
    return Credentials(
        token=token,
        base_url="https://example.invalid",
        account_id="account-1",
        user_id="user-1",
    )


def _attach_client(
    gateway: WeChatILinkGateway,
    credentials: Credentials,
) -> FakeClient:
    client = FakeClient(credentials)
    gateway._client = client
    gateway._credentials = credentials
    gateway._session_fingerprint = credential_fingerprint(credentials)
    gateway._set_status(GatewayPhase.LISTENING)
    return client


async def test_gateway_maps_all_supported_payloads_to_internal_client(tmp_path: Path) -> None:
    gateway = WeChatILinkGateway(
        settings=_settings(tmp_path),
        store=SQLiteStore(tmp_path / "bridge.db"),
        dispatcher=CaptureDispatcher(),
    )
    client = _attach_client(gateway, _credentials())

    await gateway.send_message(
        user_id="user-1",
        payload=TextPayload(text="hello"),
        context_token="context-1",
    )
    await gateway.send_message(
        user_id="user-1",
        payload=ImagePayload(data=b"image", caption="caption"),
        context_token="context-1",
    )
    await gateway.send_message(
        user_id="user-1",
        payload=FilePayload(data=b"file", file_name="report.pdf", caption=None),
        context_token="context-1",
    )
    await gateway.send_message(
        user_id="user-1",
        payload=TypingStartPayload(),
        context_token="context-1",
    )
    await gateway.send_message(
        user_id="user-1",
        payload=TypingCancelPayload(),
        context_token="context-1",
    )

    assert client.calls == [
        (
            "text",
            {
                "user_id": "user-1",
                "context_token": "context-1",
                "text": "hello",
            },
        ),
        (
            "image",
            {
                "user_id": "user-1",
                "context_token": "context-1",
                "data": b"image",
                "caption": "caption",
            },
        ),
        (
            "file",
            {
                "user_id": "user-1",
                "context_token": "context-1",
                "data": b"file",
                "file_name": "report.pdf",
                "caption": None,
            },
        ),
        (
            "typing",
            {
                "user_id": "user-1",
                "context_token": "context-1",
                "active": True,
            },
        ),
        (
            "typing",
            {
                "user_id": "user-1",
                "context_token": "context-1",
                "active": False,
            },
        ),
    ]


async def test_gateway_maps_upstream_temporary_error(tmp_path: Path) -> None:
    gateway = WeChatILinkGateway(
        settings=_settings(tmp_path),
        store=SQLiteStore(tmp_path / "bridge.db"),
        dispatcher=CaptureDispatcher(),
    )
    client = _attach_client(gateway, _credentials())
    client.send_error = ILinkApiError("sendmessage", http_status=503)

    with pytest.raises(GatewayTemporaryError):
        await gateway.send_message(
            user_id="user-1",
            payload=TextPayload(text="hello"),
            context_token="context-1",
        )


async def test_owner_inbound_binds_context_keeps_quote_and_deduplicates(
    tmp_path: Path,
) -> None:
    store = SQLiteStore(tmp_path / "bridge.db")
    await store.open()
    dispatcher = CaptureDispatcher()
    gateway = WeChatILinkGateway(
        settings=_settings(tmp_path),
        store=store,
        dispatcher=dispatcher,
    )
    credentials = _credentials()
    client = _attach_client(gateway, credentials)
    inbound = IncomingMessage(
        upstream_id="message_id:100",
        user_id="user-1",
        context_token="must-not-leak",
        text="hello",
        type="text",
        timestamp=datetime(2026, 7, 28, 12, 0, tzinfo=UTC),
        quoted_message=QuotedMessage(
            title="微信桥接",
            text="瓷器",
            type="text",
        ),
    )

    try:
        await gateway._handle_incoming(inbound, client=client)
        await gateway._handle_incoming(inbound, client=client)

        assert len(dispatcher.events) == 1
        event = dispatcher.events[0]
        assert event.sender.user_id == "user-1"
        assert event.message.text == "hello"
        assert event.quote is not None
        assert event.quote.title == "微信桥接"
        assert event.quote.text == "瓷器"
        assert "must-not-leak" not in event.model_dump_json()

        owner = await store.resolve_owner(credential_fingerprint(credentials))
        assert owner is not None
        assert owner.user_id == "user-1"
        assert owner.context_token == "must-not-leak"
    finally:
        await store.close()


async def test_inbound_media_is_downloaded_without_placeholder_or_webhook_leak(
    tmp_path: Path,
) -> None:
    store = SQLiteStore(tmp_path / "bridge.db")
    await store.open()
    dispatcher = CaptureDispatcher()
    gateway = WeChatILinkGateway(
        settings=_settings(tmp_path),
        store=store,
        dispatcher=dispatcher,
    )
    client = _attach_client(gateway, _credentials())
    client.downloaded = DownloadedMedia(
        data=b"\x89PNG\r\n\x1a\ninbound-image",
        kind=MediaKind.IMAGE,
    )
    inbound = IncomingMessage(
        upstream_id="message_id:101",
        user_id="user-1",
        context_token="private-context",
        text="[image]",
        type="image",
        timestamp=datetime(2026, 7, 28, 12, 0, tzinfo=UTC),
        media=(),
    )
    # 网关只有在解析器提供媒体引用时才下载；测试使用最小哑元组验证该分支。
    inbound = IncomingMessage(
        upstream_id=inbound.upstream_id,
        user_id=inbound.user_id,
        context_token=inbound.context_token,
        text=inbound.text,
        type=inbound.type,
        timestamp=inbound.timestamp,
        media=(
            # 测试客户端不读取引用内容，因此这里可使用一个最小合法对象。
            _image_reference(),
        ),
    )

    try:
        await gateway._handle_incoming(inbound, client=client)

        assert len(dispatcher.events) == 1
        event = dispatcher.events[0]
        assert event.message.text == ""
        assert event.media is not None
        assert event.media.data.endswith(b"inbound-image")
        assert event.media.mime_type == "image/png"
        assert "inbound-image" not in event.model_dump_json()
    finally:
        await store.close()


async def test_non_owner_and_missing_context_cannot_bind_owner(tmp_path: Path) -> None:
    store = SQLiteStore(tmp_path / "bridge.db")
    await store.open()
    dispatcher = CaptureDispatcher()
    gateway = WeChatILinkGateway(
        settings=_settings(tmp_path),
        store=store,
        dispatcher=dispatcher,
    )
    credentials = _credentials()
    client = _attach_client(gateway, credentials)

    try:
        await gateway._handle_incoming(
            IncomingMessage(
                upstream_id="message_id:102",
                user_id="unexpected-user",
                context_token="untrusted-context",
                text="claim owner",
                type="text",
                timestamp=datetime.now(UTC),
            ),
            client=client,
        )
        await gateway._handle_incoming(
            IncomingMessage(
                upstream_id="message_id:103",
                user_id="user-1",
                context_token="",
                text="no context",
                type="text",
                timestamp=datetime.now(UTC),
            ),
            client=client,
        )

        assert dispatcher.events == []
        assert await store.resolve_owner(credential_fingerprint(credentials)) is None
    finally:
        await store.close()


async def test_poll_loop_persists_cursor_after_inbound_delivery(tmp_path: Path) -> None:
    store = SQLiteStore(tmp_path / "bridge.db")
    await store.open()
    dispatcher = CaptureDispatcher()
    gateway = WeChatILinkGateway(
        settings=_settings(tmp_path),
        store=store,
        dispatcher=dispatcher,
    )
    credentials = _credentials()
    fingerprint = credential_fingerprint(credentials)
    await store.activate_session(fingerprint)
    client = _attach_client(gateway, credentials)
    gateway._cursor = "cursor-1"

    async def one_batch(_cursor: str) -> UpdateBatch:
        return UpdateBatch(
            cursor="cursor-2",
            messages=(
                IncomingMessage(
                    upstream_id="message_id:cursor-test",
                    user_id="user-1",
                    context_token="private-context",
                    text="delivered before cursor",
                    type="text",
                    timestamp=datetime.now(UTC),
                ),
            ),
        )

    async def publish_and_stop(event: InboundEvent) -> None:
        dispatcher.events.append(event)
        await gateway.stop()

    client.get_updates = one_batch  # type: ignore[method-assign]
    dispatcher.publish = publish_and_stop  # type: ignore[method-assign]

    try:
        await gateway._poll_loop()

        assert len(dispatcher.events) == 1
        assert dispatcher.events[0].message.text == "delivered before cursor"
        assert await store.get_state(f"cursor:{fingerprint}") == "cursor-2"
    finally:
        await store.close()


async def test_gateway_run_uses_stored_credentials_and_manages_full_poll_lifecycle(
    tmp_path: Path,
) -> None:
    settings = _settings(tmp_path)
    credentials = _credentials()
    await CredentialStore(settings.credentials_path).save(credentials)
    requested_paths: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requested_paths.append(request.url.path)
        if request.url.path == "/ilink/bot/getupdates":
            return httpx.Response(
                200,
                json={
                    "ret": 0,
                    "get_updates_buf": "cursor-from-server",
                    "msgs": [
                        {
                            "message_id": 5001,
                            "from_user_id": "user-1",
                            "message_type": 1,
                            "create_time_ms": 1_785_283_200_000,
                            "context_token": "private-context",
                            "item_list": [
                                {
                                    "type": 1,
                                    "text_item": {"text": "lifecycle message"},
                                }
                            ],
                        }
                    ],
                },
            )
        if request.url.path in {
            "/ilink/bot/msg/notifystart",
            "/ilink/bot/msg/notifystop",
        }:
            return httpx.Response(200, json={"ret": 0})
        raise AssertionError(request.url.path)

    http_client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    transport = ILinkTransport(http_client=http_client)
    store = SQLiteStore(settings.database_path)
    await store.open()
    dispatcher = CaptureDispatcher()
    gateway = WeChatILinkGateway(
        settings=settings,
        store=store,
        dispatcher=dispatcher,
        transport_factory=lambda: transport,
    )

    async def publish_and_stop(event: InboundEvent) -> None:
        dispatcher.events.append(event)
        await gateway.stop()

    dispatcher.publish = publish_and_stop  # type: ignore[method-assign]

    try:
        await gateway.run()

        assert gateway.status.phase is GatewayPhase.STOPPED
        assert [event.message.text for event in dispatcher.events] == ["lifecycle message"]
        fingerprint = credential_fingerprint(credentials)
        assert await store.get_state(f"cursor:{fingerprint}") == "cursor-from-server"
        assert requested_paths == [
            "/ilink/bot/msg/notifystart",
            "/ilink/bot/getupdates",
            "/ilink/bot/msg/notifystop",
        ]
    finally:
        await store.close()
        await http_client.aclose()


def test_credential_fingerprint_changes_with_authorization_session() -> None:
    first = credential_fingerprint(_credentials(token="token-a"))
    second = credential_fingerprint(_credentials(token="token-b"))

    assert first != second
    assert len(first) == 64


async def test_gateway_waits_for_admin_before_qr_and_logout_cancels_login(
    tmp_path: Path,
) -> None:
    store = SQLiteStore(tmp_path / "bridge.db")
    await store.open()
    dispatcher = CaptureDispatcher()
    transport = FakeLoginOnlyTransport()
    gateway = WeChatILinkGateway(
        settings=_settings(tmp_path),
        store=store,
        dispatcher=dispatcher,
        transport_factory=lambda: transport,  # type: ignore[arg-type,return-value]
    )
    task = asyncio.create_task(gateway.run())

    try:
        for _attempt in range(50):
            if gateway.status.phase is GatewayPhase.LOGIN_REQUIRED:
                break
            await asyncio.sleep(0.01)

        assert gateway.status.phase is GatewayPhase.LOGIN_REQUIRED
        assert not transport.qr_requested.is_set()
        assert gateway.login_qr_url is None

        await gateway.start_login()
        await asyncio.wait_for(transport.poll_started.wait(), timeout=1)

        assert gateway.status.phase is GatewayPhase.LOGGING_IN
        assert gateway.status.qr_ready is True
        assert gateway.login_qr_url == "https://weixin.qq.com/x/private-qr"

        await gateway.logout()

        assert gateway.status.phase is GatewayPhase.LOGIN_REQUIRED
        assert gateway.login_qr_url is None
        assert dispatcher.drain_calls == 1
        assert not _settings(tmp_path).credentials_path.exists()
        assert transport.closed is True
    finally:
        await gateway.stop()
        await asyncio.wait_for(task, timeout=1)
        await store.close()


def _image_reference():
    from wechat_bot_bridge.infrastructure.wechat.ilink.models import (
        CdnMedia,
        MediaReference,
    )

    return MediaReference(
        kind=MediaKind.IMAGE,
        cdn=CdnMedia(
            encrypted_query_param="fake-reference",
            aes_key="",
        ),
    )
