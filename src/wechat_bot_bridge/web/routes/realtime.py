"""管理页面 WebSocket 实时事件路由。"""

from __future__ import annotations

import asyncio

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from ...infrastructure.realtime.conversation import (
    ConversationHub,
    MessageBroadcast,
    ResetBroadcast,
    TypingBroadcast,
)
from ..schemas import ConversationMessageResponse
from ..security import AdminTokenSigner


def build_realtime_router() -> APIRouter:
    router = APIRouter()

    @router.websocket("/admin/ws")
    async def conversation_websocket(websocket: WebSocket) -> None:
        protocols = {
            value.strip()
            for value in websocket.headers.get("sec-websocket-protocol", "").split(",")
            if value.strip()
        }
        signer: AdminTokenSigner = websocket.app.state.admin_token_signer
        token_valid = any(
            signer.verify(protocol.removeprefix("wbb-token."))
            for protocol in protocols
            if protocol.startswith("wbb-token.")
        )
        if "wbb-admin" not in protocols or not token_valid:
            await websocket.close(code=4401, reason="Invalid or expired management token")
            return

        hub: ConversationHub = websocket.app.state.conversation_hub
        await websocket.accept(subprotocol="wbb-admin")
        await websocket.send_json(
            {
                "type": "connected",
                "status": websocket.app.state.gateway.status.model_dump(mode="json"),
            }
        )
        try:
            async with hub.subscribe() as queue:
                while True:
                    try:
                        event = await asyncio.wait_for(queue.get(), timeout=25)
                    except TimeoutError:
                        await websocket.send_json({"type": "ping"})
                        continue
                    await _send_broadcast(websocket, event)
        except (WebSocketDisconnect, RuntimeError):
            return

    return router


async def _send_broadcast(
    websocket: WebSocket,
    event: MessageBroadcast | TypingBroadcast | ResetBroadcast,
) -> None:
    """把内部事件映射为稳定且最小的页面协议。"""

    if isinstance(event, MessageBroadcast):
        message = ConversationMessageResponse.from_domain(event.message)
        await websocket.send_json(
            {
                "type": "message_upsert",
                "message": message.model_dump(mode="json", by_alias=True),
            }
        )
    elif isinstance(event, TypingBroadcast):
        await websocket.send_json(
            {
                "type": "typing",
                "active": event.active,
                "origin": event.origin.value,
            }
        )
    elif isinstance(event, ResetBroadcast):
        await websocket.send_json({"type": "account_reset"})
