# MCP 通知微信接入指南

## 从原 HTTP 接口迁移

原 `POST /api/v1/messages` 已移除。现在由 MCP 客户端连接运行中的 Bridge，
调用 `send_wechat_message` 向唯一微信 owner 发送通知。原 Bearer Token、已绑定账号和
聊天记录可以继续使用。原请求体放在工具参数的 `message` 字段中。

微信入站事件转发给下游仍使用单向 HTTP Webhook，由管理页单独配置。
[入站转发契约](api.md#4-单向消息转发)

## 连接 Bridge

1. 启动 Bridge。Windows 发布版双击 EXE，从托盘“打开配置目录”的 `settings.json` 查看首次生成的管理密码，再打开管理页面。源码或 Docker 部署访问 `/admin`。
2. 使用管理用户名和密码登录，完成微信扫码。用扫码账号先给 Bot 私聊一条消息，建立 owner 上下文。
3. 在管理页的“MCP Token”中复制 Bearer Token。
4. 将 MCP 客户端的传输设为 **Streamable HTTP**，地址填 `http://127.0.0.1:8000/mcp`，请求头填
   `Authorization: Bearer <从管理页复制的 Token>`。修改监听端口后，客户端地址也要对应修改。

例如，VS Code 本地客户端可以在用户的 `mcp.json` 中添加以下配置。连接时在密码框中输入 Token。

```json
{
  "servers": {
    "wechatBridge": {
      "type": "http",
      "url": "http://127.0.0.1:8000/mcp",
      "headers": {"Authorization": "Bearer ${input:bridgeToken}"}
    }
  },
  "inputs": [
    {"id": "bridgeToken", "type": "promptString", "description": "微信桥接 MCP Token", "password": true}
  ]
}
```

桥接服务与管理页共用一个端口。跨主机使用 HTTPS 反向代理或受控私网。SDK 校验 Host 和浏览器
Origin，允许本机回环地址和明确配置的 `WBB_HOST`。通过不同域名访问时，在 Bridge 配置
`WBB_MCP_PUBLIC_ORIGIN=https://bridge.example.test` 并重启。填写实际协议、域名和非默认端口，
客户端再在该地址后加 `/mcp`。令牌仅通过请求头传递，轮换后下一个请求就需要使用新值。

客户端先执行 `initialize` 和 `notifications/initialized`，再通过 `tools/list` 发现工具并用
`tools/call` 调用。服务使用无状态模式，返回 JSON，无需维护会话 ID。
[Streamable HTTP 规范](https://modelcontextprotocol.io/specification/2025-11-25/basic/transports) ·
[VS Code MCP 配置](https://code.visualstudio.com/docs/agents/reference/mcp-configuration)

## 工具参数

工具名为 `send_wechat_message`。`tools/list` 的 `inputSchema` 提供每个字段的说明、类型和必填项。工具的 `description` 列出当前部署实际接受的文字字符数和解码后媒体字节数。

| 字段 | 何时填写 | 含义与取值 |
| --- | --- | --- |
| `message` | 始终必填 | 描述一个动作的 JSON 对象。目标固定为已绑定的扫码账号，只填写所选类型的字段。 |
| `message.type` | 始终必填 | 区分大小写。`text` 发文字，`image` 发图片，`file` 发普通文件，`typing_start` 开始输入提示，`typing_cancel` 取消提示。后两种仅传 `type`。 |
| `message.text` | `text` 必填 | 正文字符串，至少含一个非空白字符。文字上限由 `WBB_MESSAGE_MAX_CHARS` 决定，默认 16000，最高 100000 字符。超过 4000 字符时会分条发送。 |
| `message.content_base64` | `image`、`file` 必填 | 原始文件字节的标准 Base64 字符串，不加换行或 `data:` URL 前缀。解码后至少 1 字节，上限由 `WBB_MEDIA_MAX_BYTES` 决定，默认 20 MiB，最高 25 MiB。 |
| `message.file_name` | `file` 必填 | 微信显示的纯文件名，例如 `report.pdf`。最多 255 字符，不能只含空白，不能含 `/`、`\` 或 NUL。图片扩展名须改用 `image`，视频扩展名不支持。 |
| `message.caption` | `image`、`file` 可选 | 说明文字。省略或传 `null` 时只发媒体。提供时须含非空白字符，最多 4000 字符，并受部署文字上限约束。说明会作为单独的文字消息先发送。 |

**多行通知建议用 Markdown**。短标题、空行和列表能让接收人快速找到结果。桥接服务会保留换行及 Markdown 标记，按微信文本发送，不做富文本转换。单句通知也可以直接写纯文本。

以下示例都是完整工具参数。图片示例包含一张 1×1 像素的 PNG，实际调用时替换为自己的图片编码。

| 动作 | 参数示例 |
| --- | --- |
| 发送文字 | `{"message":{"type":"text","text":"## 构建完成\n- 版本：v1.2.0\n- 测试：通过"}}` |
| 发送图片 | `{"message":{"type":"image","content_base64":"iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAIAAACQd1PeAAAADElEQVR4nGMQbw0BAAGnAPEglmHsAAAAAElFTkSuQmCC","caption":"运行结果"}}` |
| 发送文件 | `{"message":{"type":"file","content_base64":"SGVsbG8=","file_name":"report.txt","caption":"日报"}}` |
| 开始输入 | `{"message":{"type":"typing_start"}}` |
| 取消输入 | `{"message":{"type":"typing_cancel"}}` |

输入状态不会生成聊天气泡。任务结束、取消或超时时，调用 `typing_cancel` 收起提示。
工具不接受 `recipient`、`user_id`、出站引用或幂等字段。原生语音和视频不在支持范围内。

## Python 示例

示例使用项目锁定的 MCP Python SDK。调用方从自己的环境变量 `BRIDGE_MCP_TOKEN` 读取
管理页提供的 Token。运行 Bridge 的进程从 SQLite 读取该 Token。

```python
import asyncio
import json
import os
from datetime import timedelta

import httpx
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client


async def main() -> None:
    url = "http://127.0.0.1:8000/mcp"
    headers = {"Authorization": f"Bearer {os.environ['BRIDGE_MCP_TOKEN']}"}
    async with httpx.AsyncClient(headers=headers, timeout=90) as client:
        async with streamable_http_client(url, http_client=client) as (read, write, _):
            async with ClientSession(
                read, write, read_timeout_seconds=timedelta(seconds=90)
            ) as session:
                await session.initialize()
                result = await session.call_tool(
                    "send_wechat_message",
                    {"message": {"type": "text", "text": "## 同步完成\n- 数据表：3 张\n- 错误：0"}},
                )
                if result.isError:
                    raise RuntimeError(json.loads(result.content[0].text)["code"])
                print(result.structuredContent["id"], result.structuredContent["msg_id"])


asyncio.run(main())
```

CI 或任务完成 Hook 可以在同一调用点发送文字消息。发送图片或文件时，用
`base64.b64encode(path.read_bytes()).decode("ascii")` 填充 `content_base64`。

## 结果与错误

仅当 iLink 同步接受本次动作时，结果为 `isError: false`。成功对象位于 `structuredContent`，
同时提供 JSON 文本形式的 `content[0].text` 供客户端读取。

```json
{
  "id": "9e4431a4-d3eb-449e-b027-1ba44f77884d",
  "msg_id": "123456789",
  "type": "text",
  "status": "sent",
  "created_at": "2026-07-30T08:00:00Z",
  "sent_at": "2026-07-30T08:00:01Z"
}
```

`tools/list` 的 `outputSchema` 描述成功回执。调用方可按以下字段读取 `structuredContent`。

| 字段 | 类型或取值 | 含义 |
| --- | --- | --- |
| `id` | UUID 字符串 | 本次发送的本地审计 ID。即使正文相同，两次发送仍有不同的 ID。 |
| `msg_id` | 字符串或 `null` | 微信返回的主消息 ID。长文字对应第一段，带说明的媒体对应图片或文件本身。输入状态或上游未提供 ID 时为 `null`。 |
| `type` | 与 `message.type` 相同 | 本次执行的动作，包括 `text`、`image`、`file`、`typing_start` 或 `typing_cancel`。 |
| `status` | 固定为 `sent` | iLink 已接受动作，不能据此判断对方是否已读。 |
| `created_at` | RFC 3339 UTC 时间字符串 | 桥接服务创建本次发送审计记录的时间。 |
| `sent_at` | RFC 3339 UTC 时间字符串 | iLink 接受动作后，桥接服务记录成功的时间。 |

握手和调用若缺少有效 Bearer，HTTP 层返回 401。参数和业务错误在 MCP 工具结果中标为
`isError: true`，从 `content[0].text` 解析 JSON，例如
`{"code":"owner_not_bound","message":"The owner is not bound; send the bot a message first"}`。

| 工具错误码 | 调用方处理 |
| --- | --- |
| `unknown_tool` | 通过 `tools/list` 确认工具名为 `send_wechat_message` |
| `invalid_message` | 修正参数、文件名、Base64 或部署限制后再发送 |
| `owner_not_bound`、`interaction_required` | 让扫码账号向 Bot 私聊后再调用 |
| `wechat_not_ready` | 登录管理页完成授权，查看 `/health/ready` |
| `wechat_rejected`、`wechat_temporary_error` | 检查上游、互动窗口与限频，避免连续重发 |
| `internal_error` | 检查 Bridge 运行状态和日志，核实投递结果 |

每次工具调用都是独立发送。JSON-RPC 的请求 ID 只关联协议响应，不是幂等键。若请求已投递但
响应超时，结果未知；请先核实微信或管理页历史，再决定是否重试。Bridge 不在后台自动重发。
上游 `ret=-2` 可能与上下文过期或限频有关。
