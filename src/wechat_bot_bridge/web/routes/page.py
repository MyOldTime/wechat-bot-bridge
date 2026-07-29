"""管理页面 HTML 外壳。"""

from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, Request, Response
from fastapi.templating import Jinja2Templates

from ..security import AdminTokenSigner

_WEB_ROOT = Path(__file__).resolve().parents[1]
_TEMPLATES = Jinja2Templates(directory=_WEB_ROOT / "templates")


def build_page_router() -> APIRouter:
    router = APIRouter()

    @router.get("")
    async def chat_page(request: Request) -> Response:
        settings = request.app.state.settings
        signer: AdminTokenSigner = request.app.state.admin_token_signer
        response = _TEMPLATES.TemplateResponse(
            request=request,
            name="chat.html",
            context={
                "admin_token": signer.issue(),
                "web_username": settings.web_username,
                "message_max_chars": settings.message_max_chars,
                "media_max_bytes": settings.media_max_bytes,
                "typing_timeout_seconds": settings.web_typing_timeout_seconds,
            },
        )
        response.headers["Cache-Control"] = "no-store"
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; "
            "img-src 'self' data:; "
            "style-src 'self'; "
            "script-src 'self'; "
            "connect-src 'self' ws: wss:; "
            "object-src 'none'; "
            "base-uri 'none'; "
            "frame-ancestors 'none'"
        )
        return response

    return router
