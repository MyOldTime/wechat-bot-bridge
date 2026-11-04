"""管理端公共 Token 与单向转发配置路由。"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Header, Request, Response

from ...application.configuration import (
    OutboundForwardingConfigurationService,
    PublicApiCredentialService,
)
from ..schemas import (
    OutboundForwardingResponse,
    OutboundForwardingUpdateRequest,
    PublicApiTokenResponse,
)
from ..security import require_admin_token


def _disable_caching(response: Response) -> None:
    response.headers["Cache-Control"] = "no-store"
    response.headers["Pragma"] = "no-cache"


def build_configuration_router() -> APIRouter:
    router = APIRouter()

    @router.get(
        "/api/public-api-token",
        response_model=PublicApiTokenResponse,
    )
    async def get_public_api_token(
        request: Request,
        response: Response,
    ) -> PublicApiTokenResponse:
        service: PublicApiCredentialService = request.app.state.public_api_credentials
        snapshot = await service.current()
        _disable_caching(response)
        return PublicApiTokenResponse.from_domain(snapshot)

    @router.post(
        "/api/public-api-token/rotate",
        response_model=PublicApiTokenResponse,
    )
    async def rotate_public_api_token(
        request: Request,
        response: Response,
        admin_token: Annotated[str, Header(alias="X-Admin-Token")],
    ) -> PublicApiTokenResponse:
        require_admin_token(request, admin_token)
        service: PublicApiCredentialService = request.app.state.public_api_credentials
        snapshot = await service.rotate()
        _disable_caching(response)
        return PublicApiTokenResponse.from_domain(snapshot)

    @router.get(
        "/api/outbound-forwarding",
        response_model=OutboundForwardingResponse,
    )
    async def get_outbound_forwarding(
        request: Request,
        response: Response,
    ) -> OutboundForwardingResponse:
        service: OutboundForwardingConfigurationService = request.app.state.forwarding_configuration
        config = await service.current()
        _disable_caching(response)
        return OutboundForwardingResponse.from_domain(config)

    @router.put(
        "/api/outbound-forwarding",
        response_model=OutboundForwardingResponse,
    )
    async def update_outbound_forwarding(
        request: Request,
        response: Response,
        body: OutboundForwardingUpdateRequest,
        admin_token: Annotated[str, Header(alias="X-Admin-Token")],
    ) -> OutboundForwardingResponse:
        require_admin_token(request, admin_token)
        service: OutboundForwardingConfigurationService = request.app.state.forwarding_configuration
        config = await service.update(
            enabled=body.enabled,
            scheme=body.scheme,
            host=body.host,
            port=body.port,
            path=body.path,
            secret=body.secret,
            timeout_seconds=body.timeout_seconds,
            max_attempts=body.max_attempts,
        )
        _disable_caching(response)
        return OutboundForwardingResponse.from_domain(config)

    @router.post(
        "/api/outbound-forwarding/rotate-secret",
        response_model=OutboundForwardingResponse,
    )
    async def rotate_outbound_forwarding_secret(
        request: Request,
        response: Response,
        admin_token: Annotated[str, Header(alias="X-Admin-Token")],
    ) -> OutboundForwardingResponse:
        require_admin_token(request, admin_token)
        service: OutboundForwardingConfigurationService = request.app.state.forwarding_configuration
        config = await service.rotate_secret()
        _disable_caching(response)
        return OutboundForwardingResponse.from_domain(config)

    return router
