"""同源管理页面路由装配。"""

from __future__ import annotations

from fastapi import APIRouter, Security

from .routes.account import build_account_router
from .routes.messages import build_message_router
from .routes.page import build_page_router
from .routes.realtime import build_realtime_router
from .security import require_web_basic_auth


def build_web_router() -> APIRouter:
    """组合管理页面的 HTTP 与 WebSocket 边界。

    HTTP 页面统一继承 Basic Auth；WebSocket 继续使用短期子协议令牌，避免
    浏览器无法为 WebSocket 握手稳定提供 Basic 凭据的问题。
    """

    root = APIRouter(include_in_schema=False)
    protected = APIRouter(
        prefix="/admin",
        dependencies=[Security(require_web_basic_auth)],
    )
    protected.include_router(build_account_router())
    protected.include_router(build_message_router())
    root.include_router(
        build_page_router(),
        prefix="/admin",
        dependencies=[Security(require_web_basic_auth)],
    )
    root.include_router(protected)
    root.include_router(build_realtime_router())
    return root
