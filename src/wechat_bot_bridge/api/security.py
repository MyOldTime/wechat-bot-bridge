"""在读取 MCP 请求体或进入协议层之前鉴权，包括握手和工具发现。"""

from __future__ import annotations

import json

from fastapi.security.utils import get_authorization_scheme_param
from mcp.server.streamable_http_manager import StreamableHTTPSessionManager
from starlette.datastructures import Headers, MutableHeaders
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.types import Message, Receive, Scope, Send

from ..application.configuration import PublicApiCredentialService


class AuthenticatedMCP:
    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        request = Request(scope)
        scheme, token = get_authorization_scheme_param(request.headers.get("authorization"))
        credentials: PublicApiCredentialService = request.app.state.public_api_credentials
        if not await credentials.matches(token if scheme.lower() == "bearer" else ""):
            response = JSONResponse(
                {"detail": {"code": "unauthorized", "message": "Invalid or missing credentials"}},
                status_code=401,
                headers={"WWW-Authenticate": "Bearer"},
            )
            await response(scope, receive, send)
            return
        manager: StreamableHTTPSessionManager = request.app.state.mcp_manager
        # SDK 的 JSON 传输错误会附上 Pydantic 的原始输入或无效请求头。
        # 保留协议错误码和 HTTP 状态，丢弃可能含正文或令牌的诊断文本。
        error_status = 0

        async def safe_send(event: Message) -> None:
            nonlocal error_status
            if event["type"] == "http.response.start":
                if (
                    event["status"] in {400, 500}
                    and Headers(scope=event).get("content-type") == "application/json"
                ):
                    error_status = event["status"]
                    del MutableHeaders(scope=event)["content-length"]
            elif error_status and event["type"] == "http.response.body":
                error = json.loads(event["body"])
                detail = "Invalid MCP request" if error_status == 400 else "Internal MCP error"
                error["error"] = {"code": error["error"]["code"], "message": detail}
                event = {**event, "body": json.dumps(error).encode()}
            await send(event)

        await manager.handle_request(scope, receive, safe_send)
