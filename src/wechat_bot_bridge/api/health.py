"""不暴露业务数据的最小健康检查。"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from ..domain.models import GatewayPhase, GatewayStatus
from ..domain.ports import BridgeStore


def build_health_router() -> APIRouter:
    router = APIRouter(include_in_schema=False)

    @router.get("/health/live")
    async def live() -> dict[str, str]:
        return {"status": "ok"}

    @router.get("/health/ready")
    async def ready(request: Request) -> JSONResponse:
        store: BridgeStore = request.app.state.store
        dispatcher: Any = request.app.state.dispatcher
        gateway_status: GatewayStatus = request.app.state.gateway.status
        is_ready = (
            store.is_open and dispatcher.running and gateway_status.phase is GatewayPhase.LISTENING
        )
        return JSONResponse(
            status_code=200 if is_ready else 503,
            content={"status": "ready" if is_ready else "not_ready"},
        )

    return router
