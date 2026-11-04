"""唯一的外部 MCP 工具。协议握手与 Streamable HTTP 传输由官方 SDK 管理。"""

from __future__ import annotations

import json
from importlib.metadata import version
from typing import Any
from urllib.parse import urlsplit

from mcp import types
from mcp.server.lowlevel import Server
from mcp.server.streamable_http_manager import StreamableHTTPSessionManager
from mcp.server.transport_security import TransportSecuritySettings
from pydantic import ValidationError

from ..application.messaging import NotificationService
from ..core.config import Settings
from ..domain.errors import BridgeError
from ..domain.models import MessageOrigin
from .schemas import SendMessageResponse, SendToolArguments, to_domain_payload


def _tool_error(code: str, message: str) -> types.CallToolResult:
    error = json.dumps({"code": code, "message": message}, ensure_ascii=False)
    return types.CallToolResult(isError=True, content=[types.TextContent(type="text", text=error)])


def build_mcp_manager(
    service: NotificationService, settings: Settings
) -> StreamableHTTPSessionManager:
    server: Server[Any, Any] = Server("wechat-bot-bridge", version=version("wechat-bot-bridge"))
    tool = types.Tool(
        name="send_wechat_message",
        description=(
            "向已绑定的唯一微信账号同步发送通知。用 message.type 选择文字、图片、文件或输入状态。"
            "多行正文建议用 Markdown 排版，标记按微信文本发送。"
            f"当前部署文字上限为 {settings.message_max_chars} 字符，"
            f"媒体上限为解码后 {settings.media_max_bytes} 字节。"
            "成功回执见 structuredContent。工具失败时 isError 为 true，"
            "content[0].text 是含 code 和 message 的 JSON。每次独立发送，超时重试可能重复通知。"
        ),
        inputSchema=SendToolArguments.model_json_schema(),
        outputSchema=SendMessageResponse.model_json_schema(mode="serialization"),
        annotations=types.ToolAnnotations(readOnlyHint=False, idempotentHint=False),
    )

    @server.list_tools()
    async def list_tools() -> list[types.Tool]:
        return [tool]

    # 自行复用管理页面的模型校验，避免 SDK 的通用错误回显正文或媒体。
    @server.call_tool(validate_input=False)
    async def call_tool(
        name: str, arguments: dict[str, Any]
    ) -> dict[str, Any] | types.CallToolResult:
        if name != tool.name:
            return _tool_error("unknown_tool", "Unknown tool")
        try:
            args = SendToolArguments.model_validate(arguments)
            result = await service.send(
                payload=to_domain_payload(args.message), origin=MessageOrigin.MCP
            )
            return SendMessageResponse.from_domain(result).model_dump(mode="json", by_alias=True)
        except ValidationError:
            return _tool_error("invalid_message", "Arguments do not match the tool input schema")
        except ValueError as exc:
            return _tool_error("invalid_message", str(exc))
        except BridgeError as exc:
            return _tool_error(exc.code, str(exc))
        except Exception:
            # SDK 默认会把未捕获异常的文本发给客户端。
            return _tool_error("internal_error", "The bridge could not complete the request")

    hosts = {"localhost", "127.0.0.1", "[::1]"}
    if settings.host not in {"0.0.0.0", "::"}:
        hosts.add(f"[{settings.host}]" if ":" in settings.host else settings.host)
    allowed_hosts = sorted({value for host in hosts for value in (host, f"{host}:*")})
    allowed_origins = [
        f"{scheme}://{host}" for host in allowed_hosts for scheme in ("http", "https")
    ]
    if settings.mcp_public_origin:
        origin = str(settings.mcp_public_origin).rstrip("/")
        allowed_hosts.append(urlsplit(origin).netloc)
        allowed_origins.append(origin)
    return StreamableHTTPSessionManager(
        server,
        stateless=True,
        json_response=True,
        security_settings=TransportSecuritySettings(
            allowed_hosts=allowed_hosts,
            allowed_origins=allowed_origins,
        ),
        # Base64 膨胀与 JSON 信封/转义开销；解码后的部署上限仍由应用服务执行。
        max_request_body_size=4 * ((settings.media_max_bytes + 2) // 3) + 2 * 1024 * 1024,
    )
