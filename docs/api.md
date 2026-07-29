# HTTP 接口

## 1. 边界概览

对外业务契约只有一个接口：

```text
POST /api/v1/messages
```

它供自动化任务、Hook 和其他程序同步向唯一微信 `owner` 发送消息。`/api/v1` 使用 Bearer API Key；管理页面 `/admin` 使用完全独立的 HTTP Basic Auth。健康检查和页面内部接口不会进入 OpenAPI，因此 `/openapi.json` 只列出上述一个操作。

## 2. 外部认证

```http
Authorization: Bearer <WBB_API_KEY>
```

缺失或错误返回：

```http
HTTP/1.1 401 Unauthorized
WWW-Authenticate: Bearer
```

```json
{
  "detail": {
    "code": "unauthorized",
    "message": "Invalid or missing credentials"
  }
}
```

API Key 不得出现在 URL、请求体或日志中。生产环境应通过 TLS、反向代理或私有网络访问。

## 3. 发送消息

### `POST /api/v1/messages`

每个请求必须使用 `type` 判别消息模型。目标固定为当前唯一 `owner`，不接受 `recipient`、`user_id`、alias 或外部幂等字段。

### 文字

```json
{
  "type": "text",
  "text": "自动化任务已完成"
}
```

```bash
curl -X POST http://127.0.0.1:8000/api/v1/messages \
  -H "Authorization: Bearer $WBB_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"type":"text","text":"自动化任务已完成"}'
```

### 图片

```json
{
  "type": "image",
  "content_base64": "iVBORw0KGgo...",
  "caption": "任务截图"
}
```

`content_base64` 必须是标准 Base64，不含 `data:image/...;base64,` 前缀。`caption` 可选。

### 普通文件

```json
{
  "type": "file",
  "content_base64": "JVBERi0xLjQK...",
  "file_name": "report.pdf",
  "caption": "日报"
}
```

`file_name` 只允许纯文件名，不能含 `/`、`\` 或 NUL。图片扩展名必须使用 `image` 类型；视频发送没有纳入当前公共契约。

### 输入状态

```json
{"type":"typing_start"}
```

```json
{"type":"typing_cancel"}
```

输入状态会同步调用 iLink `getconfig` / `sendtyping`，但不会作为聊天气泡持久化，只通过实时事件短暂通知已连接页面。

### 内容限制

- 文字和说明受 `WBB_MESSAGE_MAX_CHARS` 限制，默认 16000 字符。
- 媒体解码后受 `WBB_MEDIA_MAX_BYTES` 限制，默认 20 MiB。
- schema 的绝对媒体硬上限为 25 MiB。
- 媒体会整体进入内存和 SQLite，本服务不是大文件流式网关。
- 当前不开放 `voice`、`quote` 或 `video` 出站类型。

未知字段和未知类型使用 FastAPI 标准 `422` 字段错误。

### 成功响应

```http
HTTP/1.1 200 OK
Content-Type: application/json
```

```json
{
  "id": "9e4431a4-d3eb-449e-b027-1ba44f77884d",
  "type": "text",
  "status": "sent",
  "created_at": "2026-07-29T12:00:00.123456Z",
  "sent_at": "2026-07-29T12:00:00.456789Z"
}
```

服务只在 iLink 同步接受动作后返回 `200`；它不是客户端展示或已读回执。

每次 POST 都会生成新的内部 ID。请求体不接受 `idempotency_key`，遗留 `Idempotency-Key` 请求头也不会触发去重。若调用方在响应到达前网络中断，再次 POST 可能产生重复通知；服务不会自动重试出站失败。

### 业务错误

| HTTP | `detail.code` | 含义 |
| --- | --- | --- |
| 409 | `owner_not_bound` | 扫码账号尚未向 Bot 发消息 |
| 409 | `interaction_required` | 已有 owner 但上下文缺失或需要重新互动 |
| 422 | `invalid_message` | 空白、超限或业务层载荷非法 |
| 502 | `wechat_rejected` | 微信上游拒绝 |
| 502 | `wechat_temporary_error` | 微信上游暂时失败 |
| 503 | `wechat_not_ready` | 未登录或授权会话不可用 |

格式：

```json
{
  "detail": {
    "code": "owner_not_bound",
    "message": "The owner is not bound; send the bot a message first"
  }
}
```

客户端应依据 HTTP 状态和稳定 `detail.code` 编程，不要匹配人类可读消息。上游 `ret=-2` 可能是互动上下文过期或限频，本服务不会把它误报为唯一原因。

## 4. 已移除的外部接口

以下接口不存在：

```text
GET /api/v1/messages
GET /api/v1/messages/{message_id}
GET /api/v1/recipients
GET /api/v1/wechat/status
```

外部程序提交请求前并不知道内部消息 ID，单条查询没有稳定使用价值；旧列表又没有正文，无法承担会话管理。因此完整聊天记录归入独立认证的管理页面，而不是继续扩大外部 API。

## 5. 管理页面

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

页面、历史、媒体、状态和页面发送接口均要求 Basic Auth。密码未配置时返回 `503 web_admin_disabled`；错误凭据返回 `401` 与 `WWW-Authenticate: Basic`。

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

已有有效授权文件时，常驻服务会直接恢复长轮询，管理页面无需再次扫码。授权过期后会删除失效凭据并回到 `login_required`，不会在无人访问管理页时把二维码打印到日志。

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
| `POST /admin/logout` | Basic Auth 表单退出并清空全部本地账号数据 |
| `WS /admin/ws` | 实时会话事件；子协议为 `wbb-admin, wbb-token.<token>` |

`POST /admin/logout` 是原生 `application/x-www-form-urlencoded` 表单，隐藏字段 `admin_token` 必须是当前页面签发的短期令牌。Basic Auth 仍由浏览器正常提交，因此跨站页面既不能只依靠浏览器缓存的 Basic 凭据，也不能绕过表单令牌。成功响应为 `303 Location: /admin`。

退出按顺序停止当前授权/长轮询、等待已经入队的入站 Sink 全部结束、删除 `credentials.json`，再事务性删除 `chat_messages`、`inbound_events`、`outbound_messages`、`recipients` 和 `bridge_state`。随后服务广播 `account_reset`、启动一轮新扫码并重定向回管理页。发送与退出共用账号操作锁，因此清理期间不会插入新的出站消息。

页面发送使用与外部接口相同的 `text`、`image`、`file`、`typing_start`、`typing_cancel` 判别类型。前三种可以额外携带管理端专用的引用 ID：

```json
{
  "type": "text",
  "text": "引用回复",
  "quote_message_id": "wechat-message-id"
}
```

引用目标不存在时返回 `404 quoted_message_not_found`。引用关系保存在 SQLite 和历史响应中，但腾讯当前公开发送能力没有经过验证且可用的 Bot 出站原生引用契约；真实微信会静默忽略出站 `ref_msg`。实际发往微信的是带 `【桥接引用·对方】` 或 `【桥接引用·我】` 前缀的普通文本，不能把它描述为原生引用卡片。外部 `POST /api/v1/messages` 不接受 `quote_message_id`。

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
      "origin": "external_api",
      "error_code": null,
      "quote": null,
      "occurred_at": "2026-07-29T12:00:00.123456Z",
      "updated_at": "2026-07-29T12:00:00.456789Z"
    }
  ],
  "next_cursor": null
}
```

`direction` 是 `inbound` 或 `outbound`；`origin` 是 `wechat`、`external_api` 或 `web_admin`。有引用时 `quote` 不会递归展开：

- 管理端桥接引用包含目标的 `id`、`direction`、`type`、`text`、`file_name`，`title` 为 `null`，可以定位到已加载的目标消息；
- 微信原生引用没有稳定目标 ID，`id` 和 `direction` 为 `null`，包含内置解析器提供的 `title`、`type` 与原文 `text` 快照。

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
    "origin": "external_api",
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
  "origin": "external_api"
}
```

账号退出并完成本地清理：

```json
{
  "type": "account_reset"
}
```

收到该事件后，客户端必须丢弃全部内存消息、附件、引用和分页游标，并以新的登录状态和历史接口为准。服务端会先清空每个订阅队列中的旧事件，避免退出前尚未消费的 `message_upsert` 在重置之后重新出现。

服务器空闲约 25 秒会发送 `{"type":"ping"}`。进程内实时队列允许丢弃慢客户端的最旧事件，页面必须以 SQLite 历史接口作为恢复依据。

## 6. 健康检查

```text
GET /health/live
GET /health/ready
```

两者公开但隐藏于 OpenAPI。`live` 只表示进程可响应；`ready` 还要求 SQLite、分发器和微信监听处于 `listening`。首次启动等待管理页面扫码、扫码进行中或退出后重新登录时，`ready` 返回 `503 not_ready` 是预期行为。它们不返回账号、消息或凭据内容。

## 7. 入站 Webhook

配置 `WBB_INBOUND_WEBHOOK_URL` 后，规范化入站事件会由 `WebhookSink` 转发；`WBB_INBOUND_WEBHOOK_SECRET` 必填。

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
  "occurred_at": "2026-07-29T12:00:00Z",
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

没有微信原生引用时 `quote` 为 `null`。接收方应使用原始请求体和时间戳验证签名，限制时间偏差，恒定时间比较，并按 `event_id` 去重。credential token、`context_token`、完整 iLink 原始载荷和下载后的媒体字节不会进入 Webhook。媒体仍以 `message.type` 呈现，上游遗留占位符会从 `message.text` 清除；二进制只保存在受 Basic Auth 保护的聊天历史中。
