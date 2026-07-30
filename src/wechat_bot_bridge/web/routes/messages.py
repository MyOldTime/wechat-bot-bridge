"""管理端聊天历史、媒体与发送路由。"""

from __future__ import annotations

from typing import Annotated
from urllib.parse import quote

from fastapi import APIRouter, Header, Query, Request, Response

from ...api.errors import (
    http_error,
    outbound_with_http_errors,
    send_with_http_errors,
)
from ...api.schemas import SendMessageRequest, SendMessageResponse, to_domain_payload
from ...application.messaging import NotificationService
from ...application.typing import TypingLeaseService
from ...domain.errors import InvalidCursorError
from ...domain.models import MessageOrigin, TypingCancelPayload, TypingStartPayload
from ...domain.ports import BridgeStore
from ..schemas import ConversationMessagePageResponse
from ..security import require_admin_token


def build_message_router() -> APIRouter:
    router = APIRouter()

    @router.get(
        "/api/messages",
        response_model=ConversationMessagePageResponse,
    )
    async def list_chat_messages(
        request: Request,
        cursor: Annotated[str | None, Query(min_length=1, max_length=1024)] = None,
        limit: Annotated[int, Query(ge=1, le=100)] = 60,
    ) -> ConversationMessagePageResponse:
        store: BridgeStore = request.app.state.store
        try:
            page = await store.list_conversation_messages(cursor=cursor, limit=limit)
        except InvalidCursorError as exc:
            raise http_error(422, exc.code, str(exc)) from exc
        return ConversationMessagePageResponse.from_domain(page)

    @router.get("/api/messages/{message_id}/media")
    async def get_chat_media(request: Request, message_id: str) -> Response:
        store: BridgeStore = request.app.state.store
        media = await store.get_conversation_media(message_id)
        if media is None:
            raise http_error(404, "media_not_found", "The message media was not found")
        disposition = "inline"
        if media.file_name is not None:
            encoded_name = quote(media.file_name, safe="")
            disposition = f"attachment; filename*=UTF-8''{encoded_name}"
        return Response(
            content=media.data,
            media_type=media.mime_type,
            headers={
                "Cache-Control": "private, max-age=3600",
                "Content-Disposition": disposition,
                "Content-Security-Policy": "default-src 'none'; sandbox",
                "X-Content-Type-Options": "nosniff",
            },
        )

    @router.post(
        "/api/messages",
        response_model=SendMessageResponse,
        response_model_exclude={"msg_id"},
    )
    async def send_from_web(
        request: Request,
        body: SendMessageRequest,
        admin_token: Annotated[str, Header(alias="X-Admin-Token")],
    ) -> SendMessageResponse:
        require_admin_token(request, admin_token)
        payload = to_domain_payload(body)
        typing_lease: TypingLeaseService = request.app.state.typing_lease
        if isinstance(payload, TypingStartPayload):
            message = await outbound_with_http_errors(typing_lease.start)
            return SendMessageResponse.from_domain(message)
        if isinstance(payload, TypingCancelPayload):
            message = await outbound_with_http_errors(typing_lease.stop)
            return SendMessageResponse.from_domain(message)

        # 正文优先：取消输入状态失败不会吞掉用户真正要发送的消息。
        await typing_lease.cancel_if_active()
        service: NotificationService = request.app.state.notification_service
        message = await send_with_http_errors(
            service=service,
            payload=payload,
            origin=MessageOrigin.WEB_ADMIN,
        )
        return SendMessageResponse.from_domain(message)

    return router
