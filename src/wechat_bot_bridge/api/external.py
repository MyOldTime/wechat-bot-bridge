"""供自动化任务和外部 Hook 调用的唯一业务接口。"""

from __future__ import annotations

from fastapi import APIRouter, Request, Security, status

from ..application.messaging import NotificationService
from ..domain.models import MessageOrigin
from .errors import send_with_http_errors
from .schemas import SendMessageRequest, SendMessageResponse, to_domain_payload
from .security import require_public_api_token


def build_external_router() -> APIRouter:
    router = APIRouter(
        prefix="/api/v1",
        dependencies=[Security(require_public_api_token)],
    )

    @router.post(
        "/messages",
        response_model=SendMessageResponse,
        status_code=status.HTTP_200_OK,
        tags=["external-messages"],
    )
    async def receive_external_message(
        request: Request,
        body: SendMessageRequest,
    ) -> SendMessageResponse:
        service: NotificationService = request.app.state.notification_service
        message = await send_with_http_errors(
            service=service,
            payload=to_domain_payload(body),
            origin=MessageOrigin.EXTERNAL_API,
        )
        return SendMessageResponse.from_domain(message)

    return router
