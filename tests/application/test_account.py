"""账号退出与消息发送的串行编排测试。"""

from __future__ import annotations

import asyncio
from pathlib import Path

from wechat_bot_bridge.application.account import AccountLifecycleService
from wechat_bot_bridge.application.messaging import NotificationService
from wechat_bot_bridge.domain.models import (
    GatewayPhase,
    GatewayStatus,
    MessageOrigin,
    OutboundPayload,
    TextPayload,
)
from wechat_bot_bridge.infrastructure.persistence.sqlite import SQLiteStore
from wechat_bot_bridge.infrastructure.realtime.conversation import (
    ConversationHub,
    ResetBroadcast,
)


class BlockingGateway:
    def __init__(self) -> None:
        self.send_started = asyncio.Event()
        self.release_send = asyncio.Event()
        self.logout_calls = 0
        self.start_login_calls = 0

    @property
    def status(self) -> GatewayStatus:
        return GatewayStatus(
            phase=GatewayPhase.LISTENING,
            authenticated=True,
            listener_running=True,
        )

    @property
    def session_fingerprint(self) -> str | None:
        return "session-a"

    @property
    def login_qr_url(self) -> str | None:
        return None

    async def run(self) -> None:
        return None

    async def stop(self) -> None:
        return None

    async def start_login(self) -> None:
        self.start_login_calls += 1

    async def submit_verification_code(self, code: str) -> None:
        del code

    async def logout(self) -> None:
        self.logout_calls += 1

    async def send_message(
        self,
        *,
        user_id: str,
        payload: OutboundPayload,
        context_token: str,
    ) -> None:
        assert user_id == "owner-user"
        assert context_token == "private-context"
        assert isinstance(payload, TextPayload)
        self.send_started.set()
        await self.release_send.wait()


async def test_logout_waits_for_inflight_send_then_clears_and_resets(
    tmp_path: Path,
) -> None:
    store = SQLiteStore(tmp_path / "bridge.db")
    await store.open()
    await store.activate_session("session-a")
    await store.save_owner_context(
        user_id="owner-user",
        context_token="private-context",
        credential_fingerprint="session-a",
    )
    gateway = BlockingGateway()
    hub = ConversationHub()
    operation_lock = asyncio.Lock()
    notifications = NotificationService(
        store=store,
        gateway=gateway,
        publisher=hub,
        message_max_chars=1000,
        media_max_bytes=1024,
        operation_lock=operation_lock,
    )
    accounts = AccountLifecycleService(
        store=store,
        gateway=gateway,
        publisher=hub,
        operation_lock=operation_lock,
    )

    try:
        async with hub.subscribe() as queue:
            sending = asyncio.create_task(
                notifications.send(
                    payload=TextPayload(text="退出前在途消息"),
                    origin=MessageOrigin.WEB_ADMIN,
                )
            )
            await gateway.send_started.wait()
            resetting = asyncio.create_task(accounts.logout_and_restart_login())
            await asyncio.sleep(0)
            assert gateway.logout_calls == 0

            gateway.release_send.set()
            await sending
            await resetting

            event = await queue.get()
            assert isinstance(event, ResetBroadcast)
            assert queue.empty()

        assert gateway.logout_calls == 1
        assert gateway.start_login_calls == 1
        assert (await store.list_conversation_messages(cursor=None, limit=10)).items == ()
        assert await store.get_active_fingerprint() is None
    finally:
        await store.close()
