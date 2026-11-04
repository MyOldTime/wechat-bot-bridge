# MCP、HTTP 管理接口与单向转发

## 1. 对外发送

外部客户端通过 Streamable HTTP 连接 `/mcp`。握手、工具发现和调用均要求
`Authorization: Bearer <MCP Token>`。缺失、错误或已轮换的 Token 返回 HTTP 401
与 `WWW-Authenticate: Bearer`。访问 `/admin` 使用独立的 Basic Auth。

`tools/list` 公布 `send_wechat_message` 的 `inputSchema` 和 `outputSchema`，包括五种动作的必填字段、可选字段和回执含义。工具说明列出当前部署的文字与媒体上限。
多行 `message.text` 建议使用 Markdown，例如 `{"message":{"type":"text","text":"## 任务完成\n- 测试通过"}}`。正文按微信文本发送，保留换行和标记。
工具同步发送到唯一 owner，成功结果包括内部 ID 和微信回执，执行失败由 `isError` 标记。每次调用独立，重复调用可能重复投递。

管理员可在“MCP Token”页面复制或轮换首次启动生成的 Token。
`/openapi.json` 的 `paths` 为空。工具契约、SDK 示例与旧 HTTP 调用的迁移步骤见
[MCP 接入指南](external-api.md)。

## 2. 管理页面

### 管理员认证

浏览器访问：

```text
GET /admin
```

使用：

```text
WBB_WEB_USERNAME
WBB_WEB_PASSWORD
```

页面、历史、媒体、状态和页面发送接口均要求 Basic Auth。Windows EXE 首次启动自动生成管理密码，
可通过托盘“打开配置目录”，在 `settings.json` 中查看上述两项。源码或 Docker 部署未配置密码时返回
`503 web_admin_disabled`；错误凭据返回 `401` 与 `WWW-Authenticate: Basic`。

页面响应签发短期随机 HMAC 令牌：

- 页面写操作通过 `X-Admin-Token` 发送；
- WebSocket 通过 `Sec-WebSocket-Protocol` 子协议头携带令牌，避免查询串进入访问日志；
- 令牌有效期由 `WBB_WEB_TOKEN_TTL_SECONDS` 控制；
- 令牌只能由已经通过 Basic Auth 的页面取得，不替代页面 HTTP 的 Basic Auth。

### 微信扫码登录

服务没有授权文件时仍保持进程存活，网关状态为 `login_required`，不会在容器终端主动申请或打印二维码。已经通过 Basic Auth 的页面会调用登录接口，随后轮询状态并从受保护的图片接口加载二维码：

1. `POST /admin/api/login` 请求启动登录，需要 `X-Admin-Token`；
2. 状态变为 `logging_in` 且 `qr_ready=true` 后，页面加载 `GET /admin/api/login/qr`；
3. 二维码接口返回 `image/svg+xml` 和 `Cache-Control: no-store`，不会返回或记录二维码原始 URL；
4. 扫码后若 `verification_required=true`，页面通过 `POST /admin/api/login/verification` 提交微信显示的纯数字配对码；
5. 授权完成后状态变为 `listening`，授权文件原子写入映射的数据目录。

存在有效授权文件时，常驻服务会直接启动长轮询，管理页面无需再次扫码。授权过期后会删除失效凭据并回到 `login_required`，二维码内容不会写入日志。

### 页面内部接口

这些路径服务于同仓库前端、隐藏于 OpenAPI，并不承诺作为外部集成 API：

| 方法与路径 | 用途 |
| --- | --- |
| `GET /admin/api/messages?limit=60&cursor=...` | 读取完整聊天记录 |
| `POST /admin/api/messages` | 从页面发送；另需 `X-Admin-Token` |
| `GET /admin/api/messages/{id}/media` | 读取受保护的图片或文件 |
| `GET /admin/api/status` | 读取脱敏网关状态 |
| `POST /admin/api/login` | 启动扫码登录；另需 `X-Admin-Token` |
| `GET /admin/api/login/qr` | 获取 `no-store` 的二维码 SVG |
| `POST /admin/api/login/verification` | 提交配对数字；另需 `X-Admin-Token` |
| `GET /admin/api/public-api-token` | 读取完整 MCP Token；响应 `no-store` |
| `POST /admin/api/public-api-token/rotate` | 轮换公共 Token；另需 `X-Admin-Token` |
| `GET /admin/api/outbound-forwarding` | 读取单向转发配置和 HMAC Secret；响应 `no-store` |
| `PUT /admin/api/outbound-forwarding` | 保存并立即应用单向转发配置；另需 `X-Admin-Token` |
| `POST /admin/api/outbound-forwarding/rotate-secret` | 轮换转发 Secret；另需 `X-Admin-Token` |
| `POST /admin/logout` | Basic Auth 表单退出并清空全部本地账号数据 |
| `WS /admin/ws` | 实时会话事件；子协议为 `wbb-admin, wbb-token.<token>` |

Token 和转发 Secret 的读取响应包含完整值，只允许由已经通过 Basic Auth 的
管理页面调用，并设置 `Cache-Control: no-store`。写操作同时要求页面现有的
`X-Admin-Token`，登录后不再弹出密码确认。转发配置请求结构：

```json
{
  "enabled": true,
  "scheme": "http",
  "host": "127.0.0.1",
  "port": 8765,
  "path": "/v1/sources/wechat-bridge/events",
  "secret": "至少 32 字符的 HMAC Secret",
  "timeout_seconds": 8,
  "max_attempts": 3
}
```

只允许 `http` / `https`；地址字段不接受 userinfo 或完整 URL；路径必须以 `/`
开头且不能含 query/fragment。保存和轮换均对下一条微信入站事件立即生效。

`POST /admin/logout` 是原生 `application/x-www-form-urlencoded` 表单，隐藏字段 `admin_token` 必须是当前页面签发的短期令牌。Basic Auth 仍由浏览器正常提交，因此跨站页面既不能只依靠浏览器缓存的 Basic 凭据，也不能绕过表单令牌。成功响应为 `303 Location: /admin`。

退出按顺序停止当前授权/长轮询、等待已经入队的入站 Sink 全部结束、删除 `credentials.json`，再事务性删除 `chat_messages`、`inbound_events`、`outbound_messages`、`recipients` 和 `bridge_state`。`service_settings` 中的公共 Token 和转发配置不会删除。随后服务广播 `account_reset`、启动一轮新扫码并重定向回管理页。发送与退出共用账号操作锁，因此清理期间不会插入新的出站消息。

页面发送使用与外部接口相同的 `text`、`image`、`file`、`typing_start`、`typing_cancel` 判别类型。管理页面和外部接口均不接受 `quote_msg_id`、`quote_message_id` 或其他出站引用字段；页面多余字段按请求校验错误返回 HTTP 422，MCP 调用返回 `invalid_message` 工具错误。

前端监听非空输入框并发送 `typing_start`，每次继续输入都会刷新定时器；清空、停止输入、发送正文或超时都会取消。服务端另以 `WBB_WEB_TYPING_TIMEOUT_SECONDS`（默认 5 秒）维护租约，即使页面断开也会自动调用 `typing_cancel`。

历史页从最新记录向前使用 `(occurred_at, id)` 不透明游标分页，每页内部按时间正序返回：

```json
{
  "items": [
    {
      "id": "9e4431a4-d3eb-449e-b027-1ba44f77884d",
      "direction": "outbound",
      "type": "text",
      "text": "自动化任务已完成",
      "file_name": null,
      "mime_type": null,
      "media_url": null,
      "status": "sent",
      "origin": "mcp",
      "error_code": null,
      "quote": null,
      "occurred_at": "2026-07-30T12:00:00.123456Z",
      "updated_at": "2026-07-30T12:00:00.456789Z"
    }
  ],
  "next_cursor": null
}
```

`direction` 是 `inbound` 或 `outbound`；`origin` 是 `wechat`、`mcp` 或 `web_admin`，升级前的外部消息保留 `external_api`。只有微信入站原生引用会让 `quote` 非空，其中包含 `message_id`、`title`、`type` 与原文 `text` 快照。服务用官方 `ref_msg.message_item.msg_id` 精确匹配入站顶层 `WeixinMessage.message_id`、入站 `MessageItem.msg_id` 或 Bot 出站主消息 ID；命中时返回管理端 `message_id`，并可用目标记录回填上游省略的原文。无法精确匹配时 `message_id` 为 `null`，不会按正文或时间猜测。普通消息和所有出站消息自身的 `quote` 都是 `null`，原始协议 message/item ID 不会出现在响应中。

```json
{
  "message_id": "f1c3a2...",
  "title": null,
  "type": "text",
  "text": "被引用的原文"
}
```

成功下载的微信入站图片或文件会提供 `media_url`；该链接仍要求同一 Basic Auth，图片以内联响应返回，带文件名的内容使用附件下载。内置解析器不生成 `[image]` / `[图片]` 占位符，也不会把图片 URL 作为说明重复展示。响应不包含 owner `user_id`、`context_token` 或授权材料。

### WebSocket 事件

连接成功：

```json
{
  "type": "connected",
  "status": {
    "phase": "listening",
    "authenticated": true,
    "listener_running": true,
    "qr_ready": false,
    "verification_required": false,
    "error_code": null
  }
}
```

消息新增或状态更新：

```json
{
  "type": "message_upsert",
  "message": {
    "id": "...",
    "direction": "outbound",
    "type": "text",
    "text": "任务完成",
    "status": "sending",
    "origin": "mcp",
    "occurred_at": "...",
    "updated_at": "..."
  }
}
```

同一出站 ID 通常先收到 `sending`，随后收到 `sent` 或 `failed`。微信入站会收到 `received`。客户端应按 ID upsert，不应把状态事件当作多条消息。

输入状态：

```json
{
  "type": "typing",
  "active": true,
  "origin": "mcp"
}
```

账号退出并完成本地清理：

```json
{
  "type": "account_reset"
}
```

收到该事件后，客户端必须丢弃全部内存消息、附件、引用和分页游标，并以新的登录状态和历史接口为准。服务端会先清空每个订阅队列中的待消费事件，避免退出前尚未消费的 `message_upsert` 在重置之后重新出现。

服务器空闲约 25 秒会发送 `{"type":"ping"}`。进程内实时队列允许丢弃慢客户端的最旧事件，页面必须以 SQLite 历史接口作为恢复依据。

## 3. 健康检查

```text
GET /health/live
GET /health/ready
```

两者公开但隐藏于 OpenAPI。`live` 只表示进程可响应；`ready` 还要求 SQLite、分发器和微信监听处于 `listening`。首次启动等待管理页面扫码、扫码进行中或退出后重新登录时，`ready` 返回 `503 not_ready` 是预期行为。它们不返回账号、消息或凭据内容。

## 4. 单向消息转发

转发默认关闭。在 `/admin` 左侧“消息转发”页面配置并启用后，规范化微信入站
事件由 `OutboundForwardingSink` 主动发送到目标 HTTP 服务。页面配置包含
scheme、host、port、path、HMAC Secret、单次超时和最大尝试次数；保存后
立即生效，不读取转发相关环境变量。

该功能只发送微信入站事件，不接收目标服务的执行进度或结果。目标服务需要
通知微信时调用 MCP 工具 `send_wechat_message`。

请求头：

```http
Content-Type: application/json
X-WeChat-Bridge-Event: <event_id>
X-WeChat-Bridge-Timestamp: <Unix 秒>
X-WeChat-Bridge-Signature: sha256=<hex digest>
```

签名：

```text
HMAC-SHA256(secret, timestamp + "." + raw_request_body)
```

事件示例：

```json
{
  "schema_version": "1",
  "event_id": "f1c3a2...",
  "source": "wechat_ilink",
  "occurred_at": "2026-07-30T12:00:00Z",
  "sender": {
    "user_id": "..."
  },
  "message": {
    "type": "text",
    "text": "..."
  },
  "quote": {
    "title": "微信桥接",
    "type": "text",
    "text": "被引用的原文"
  }
}
```

没有微信原生引用时 `quote` 为 `null`。转发事件保留 iLink 当次提供的引用快照，不包含内部协议 item ID、管理端目标消息 ID 或仅在 SQLite 历史中完成的原文回填。接收方应使用原始请求体和时间戳验证签名，限制时间偏差，恒定时间比较，并按 `event_id` 去重。credential token、`context_token`、完整 iLink 原始载荷和下载后的媒体字节不会进入转发请求。媒体仍以 `message.type` 呈现，iLink 媒体占位符会从 `message.text` 清除；二进制只保存在受 Basic Auth 保护的聊天历史中。

转发复用有界进程内入站队列。每个事件最多尝试页面配置的次数，使用有界
退避且不跟随重定向；进程重启后不续投。转发失败不回滚聊天历史，也不阻塞
其他 Sink。接收方应快速返回、按 `event_id` 去重，并自行决定是否持久化。
