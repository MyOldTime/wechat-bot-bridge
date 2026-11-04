"""外部调用接口的 Bearer 认证。"""

from __future__ import annotations

from typing import Annotated

from fastapi import HTTPException, Request, Security, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from ..application.configuration import PublicApiCredentialService

bearer = HTTPBearer(auto_error=False)


async def require_public_api_token(
    request: Request,
    credentials: Annotated[HTTPAuthorizationCredentials | None, Security(bearer)],
) -> None:
    provided = (
        credentials.credentials
        if credentials is not None and credentials.scheme.lower() == "bearer"
        else ""
    )
    credentials_service: PublicApiCredentialService = request.app.state.public_api_credentials
    if not await credentials_service.matches(provided):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={"code": "unauthorized", "message": "Invalid or missing credentials"},
            headers={"WWW-Authenticate": "Bearer"},
        )
