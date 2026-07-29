"""管理端微信登录、状态与退出路由。"""

from __future__ import annotations

import asyncio
import logging
from typing import Annotated

from fastapi import APIRouter, Header, Request, Response
from fastapi.responses import RedirectResponse

from ...api.errors import http_error
from ...application.account import AccountLifecycleService
from ...application.typing import TypingLeaseService
from ...domain.errors import GatewayNotReadyError
from ...domain.models import GatewayStatus
from ..qr import render_login_qr_svg
from ..schemas import AdminVerificationCodeRequest
from ..security import admin_token_from_form, require_admin_token

logger = logging.getLogger(__name__)


def build_account_router() -> APIRouter:
    router = APIRouter()

    @router.get("/api/status", response_model=GatewayStatus)
    async def get_wechat_status(request: Request) -> GatewayStatus:
        return request.app.state.gateway.status

    @router.post("/api/login", response_model=GatewayStatus)
    async def start_wechat_login(
        request: Request,
        admin_token: Annotated[str, Header(alias="X-Admin-Token")],
    ) -> GatewayStatus:
        require_admin_token(request, admin_token)
        lifecycle: AccountLifecycleService = request.app.state.account_lifecycle
        try:
            await lifecycle.start_login()
        except GatewayNotReadyError as exc:
            raise http_error(503, exc.code, str(exc)) from exc
        return request.app.state.gateway.status

    @router.get("/api/login/qr")
    async def get_wechat_login_qr(request: Request) -> Response:
        content: str | None = request.app.state.gateway.login_qr_url
        if content is None:
            raise http_error(404, "login_qr_not_ready", "The WeChat login QR is not ready")
        return Response(
            content=render_login_qr_svg(content),
            media_type="image/svg+xml",
            headers={
                "Cache-Control": "no-store",
                "Content-Security-Policy": "default-src 'none'; sandbox",
                "X-Content-Type-Options": "nosniff",
            },
        )

    @router.post("/api/login/verification", response_model=GatewayStatus)
    async def submit_wechat_verification_code(
        request: Request,
        body: AdminVerificationCodeRequest,
        admin_token: Annotated[str, Header(alias="X-Admin-Token")],
    ) -> GatewayStatus:
        require_admin_token(request, admin_token)
        lifecycle: AccountLifecycleService = request.app.state.account_lifecycle
        try:
            await lifecycle.submit_verification_code(body.code)
        except GatewayNotReadyError as exc:
            raise http_error(409, exc.code, str(exc)) from exc
        except ValueError as exc:
            raise http_error(422, "invalid_verification_code", str(exc)) from exc
        return request.app.state.gateway.status

    @router.post("/logout")
    async def logout_wechat(request: Request) -> RedirectResponse:
        admin_token = await admin_token_from_form(request)
        require_admin_token(request, admin_token)
        typing_lease: TypingLeaseService = request.app.state.typing_lease
        await typing_lease.cancel_if_active()
        lifecycle: AccountLifecycleService = request.app.state.account_lifecycle
        try:
            await lifecycle.logout_and_restart_login()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.exception("管理页面重置微信账号失败")
            raise http_error(
                500,
                "logout_failed",
                "The WeChat account could not be fully cleared",
            ) from exc
        return RedirectResponse(url="/admin", status_code=303)

    return router
