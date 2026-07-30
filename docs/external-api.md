# 外部消息接口对接指南

本文面向自动化任务、CI/CD、定时任务、告警系统和其他业务程序，说明如何通过
`wechat-bot-bridge` 向唯一微信 owner 发送通知。

## 1. 接口概览

| 项目 | 值 |
| --- | --- |
| 方法 | `POST` |
| 路径 | `/api/v1/messages` |
| 认证 | `Authorization: Bearer <WBB_API_KEY>` |
| 请求格式 | `application/json` |
| 成功状态 | `200 OK` |
| 发送模式 | 同步 |
| 收件人 | 当前授权账号对应的唯一 `owner` |

服务的 OpenAPI 定义位于 `/openapi.json`，交互式页面位于 `/docs`。公开业务
契约只包含这个 POST；聊天记录、管理状态和登录操作属于 `/admin` 管理端。

## 2. 接入前准备

调用前应满足：

1. 服务进程正常，`GET /health/live` 返回 `200 {"status":"ok"}`；
2. 管理员已访问 `/admin` 并完成微信扫码授权；
3. 完成扫码的微信账号已主动向 Bot 发送至少一条私聊消息；
4. `GET /health/ready` 返回 `200 {"status":"ready"}`；
5. 调用方已取得服务端配置的 `WBB_API_KEY`。

扫码只完成账号授权。第 3 步用于建立主动发送所需的唯一 owner 会话上下文，
因此不能省略。接口不接受收件人 ID、联系人名称或收件人列表。

建议调用方配置：

```text
WBB_BRIDGE_URL=http://127.0.0.1:8000
WBB_API_KEY=<与服务端相同的高熵密钥>
```

跨主机调用必须使用 HTTPS 反向代理或受控私网，不要通过明文公网 HTTP 传输
Bearer Key、消息正文或媒体内容。

## 3. 认证

每次请求必须携带：

```http
Authorization: Bearer <WBB_API_KEY>
Content-Type: application/json
```

认证失败：

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

API Key 不应放入 URL、请求体、代码仓库、构建日志或异常消息。服务端 API Key
至少 16 个字符，并且应与管理页面密码、Webhook 密钥分开生成。

## 4. 请求模型

所有请求都使用 `type` 作为判别字段。未知字段会被拒绝。

### 4.1 文字

```json
{
  "type": "text",
  "text": "自动化任务已完成"
}
```

约束：

- `text` 必填；
- 不能是空字符串或纯空白；
- 默认部署上限为 16000 字符，由 `WBB_MESSAGE_MAX_CHARS` 控制；
- HTTP schema 的绝对上限为 100000 字符；
- 超过 4000 字符时，服务按自然边界拆成多个微信文字气泡。

### 4.2 图片

```json
{
  "type": "image",
  "content_base64": "iVBORw0KGgoAAAANSUhEUgAA...",
  "caption": "任务执行截图"
}
```

约束：

- `content_base64` 是标准 Base64 字符串；
- 不得包含 `data:image/png;base64,` 等 data URL 前缀；
- `caption` 可选，不能是空白，HTTP schema 最多 4000 字符，同时受
  `WBB_MESSAGE_MAX_CHARS` 限制；
- 解码后媒体默认上限为 20 MiB，由 `WBB_MEDIA_MAX_BYTES` 控制；
- 解码后媒体的 HTTP schema 绝对上限为 25 MiB。

### 4.3 普通文件

```json
{
  "type": "file",
  "content_base64": "JVBERi0xLjQKJ...",
  "file_name": "report.pdf",
  "caption": "任务报告"
}
```

约束：

- `content_base64` 规则与图片相同；
- `file_name` 必填，最长 255 个字符；
- `file_name` 只能是文件名，不能包含 `/`、`\` 或 NUL；
- 图片扩展名必须使用 `type=image`；
- 视频扩展名和视频发送不在外部契约中；
- `caption` 可选，不能是空白，HTTP schema 最多 4000 字符，同时受
  `WBB_MESSAGE_MAX_CHARS` 限制。

### 4.4 开始输入

```json
{
  "type": "typing_start"
}
```

### 4.5 取消输入

```json
{
  "type": "typing_cancel"
}
```

输入状态只控制微信中的“正在输入”提示，不产生聊天气泡。调用方发送
`typing_start` 后应在任务结束、取消或超时时发送 `typing_cancel`。

## 5. 调用示例

### 5.1 curl 发送文字

```bash
curl --fail-with-body \
  -X POST "${WBB_BRIDGE_URL}/api/v1/messages" \
  -H "Authorization: Bearer ${WBB_API_KEY}" \
  -H "Content-Type: application/json" \
  -d '{"type":"text","text":"自动化任务已完成"}'
```

### 5.2 PowerShell 发送文字

```powershell
$headers = @{
    Authorization = "Bearer $env:WBB_API_KEY"
}
$body = @{
    type = "text"
    text = "自动化任务已完成"
} | ConvertTo-Json

Invoke-RestMethod `
    -Method Post `
    -Uri "$env:WBB_BRIDGE_URL/api/v1/messages" `
    -Headers $headers `
    -ContentType "application/json" `
    -Body $body
```

### 5.3 Python 通用客户端

```python
from __future__ import annotations

import os
from typing import Any

import httpx

BRIDGE_URL = os.environ["WBB_BRIDGE_URL"].rstrip("/")
API_KEY = os.environ["WBB_API_KEY"]


def send_wechat(payload: dict[str, Any]) -> dict[str, Any]:
    response = httpx.post(
        f"{BRIDGE_URL}/api/v1/messages",
        headers={"Authorization": f"Bearer {API_KEY}"},
        json=payload,
        timeout=90.0,
    )
    response.raise_for_status()
    return response.json()


result = send_wechat(
    {
        "type": "text",
        "text": "数据同步任务已完成",
    }
)
print(result["id"], result["msg_id"])
```

### 5.4 Python 发送图片

```python
import base64
from pathlib import Path

image_base64 = base64.b64encode(Path("result.png").read_bytes()).decode("ascii")
result = send_wechat(
    {
        "type": "image",
        "content_base64": image_base64,
        "caption": "任务结果",
    }
)
```

### 5.5 自动化任务完成 Hook

```python
from __future__ import annotations

import traceback


def run_task() -> None:
    # 在这里执行真实任务。
    ...


try:
    run_task()
except Exception:
    send_wechat(
        {
            "type": "text",
            "text": "自动化任务失败\n" + traceback.format_exc(limit=3),
        }
    )
    raise
else:
    send_wechat({"type": "text", "text": "自动化任务执行成功"})
```

不要把可能包含密码、访问令牌或个人数据的完整异常直接发送到微信。生产任务应
先对错误信息脱敏，并限制消息长度。

## 6. 成功响应

文字、图片或文件的成功响应：

```json
{
  "id": "9e4431a4-d3eb-449e-b027-1ba44f77884d",
  "msg_id": "9223372036854000",
  "type": "text",
  "status": "sent",
  "created_at": "2026-07-30T08:00:00.123456Z",
  "sent_at": "2026-07-30T08:00:00.456789Z"
}
```

| 字段 | 类型 | 含义 |
| --- | --- | --- |
| `id` | string | 服务内部生成的审计 ID |
| `msg_id` | string 或 null | 微信为本次主消息分配的服务端 ID |
| `type` | string | 本次请求的消息类型 |
| `status` | `"sent"` | iLink 已同步接受本次动作 |
| `created_at` | RFC 3339 时间 | 本地审计记录创建时间 |
| `sent_at` | RFC 3339 时间 | 上游调用成功完成时间 |

`typing_start` 和 `typing_cancel` 不产生微信聊天消息，因此 `msg_id` 为
`null`。`200 sent` 表示微信 iLink 已接受动作，不表示接收端已经展示或已读。

长文字拆成多个气泡时，响应中的 `msg_id` 对应第一段。图片或文件带
`caption` 时，说明文字先单独发送，响应中的 `msg_id` 对应媒体主消息；如果
说明已发送而媒体上传或投递失败，接口仍返回失败，因此媒体请求不是跨多条
微信消息的原子事务。

原始 `msg_id` 只在本次认证响应中返回。服务不提供外部消息查询接口，也不支持
调用方使用该 ID 主动发送原生引用；需要审计时，调用方应自行保存成功响应。

## 7. 错误处理

### 7.1 稳定业务错误

| HTTP | `detail.code` | 含义 | 建议处理 |
| --- | --- | --- | --- |
| 401 | `unauthorized` | API Key 缺失或错误 | 修正认证，不重试 |
| 409 | `owner_not_bound` | owner 尚未建立 | 让扫码账号先向 Bot 私聊 |
| 409 | `interaction_required` | 会话上下文缺失或需要互动 | 让 owner 再向 Bot 发消息 |
| 422 | `invalid_message` | 空白、超限或部署级校验失败 | 修正请求，不重试 |
| 502 | `wechat_rejected` | 微信上游拒绝本次动作 | 检查互动状态和频率 |
| 502 | `wechat_temporary_error` | 微信上游或网络暂时失败 | 谨慎人工或延迟重试 |
| 503 | `wechat_not_ready` | 微信未登录或监听未就绪 | 完成登录并检查 ready |

稳定业务错误格式：

```json
{
  "detail": {
    "code": "owner_not_bound",
    "message": "The owner is not bound; send the bot a message first"
  }
}
```

客户端应依据 HTTP 状态和 `detail.code` 判断，不要匹配 `message` 文本。

### 7.2 请求结构错误

缺少字段、未知 `type`、多余字段、非法 Base64 或错误字段类型由 FastAPI
返回 `422`，其 `detail` 是字段错误数组：

```json
{
  "detail": [
    {
      "type": "missing",
      "loc": ["body", "text"],
      "msg": "Field required",
      "input": {
        "type": "text"
      }
    }
  ]
}
```

调用方应把这类错误视为编程或配置错误，不应自动重试。

## 8. 幂等、超时与重试

接口把每次 POST 视为一条新的独立消息：

- 请求体不接受 `idempotency_key`；
- `Idempotency-Key` 请求头不参与去重；
- 相同正文的两次 POST 会发送两条消息；
- 服务端不会后台自动重试失败的出站动作。

如果 iLink 已接受消息，但 HTTP 响应在到达调用方前断开，调用方看到的是
“结果未知”。再次 POST 可能造成重复通知。建议：

1. 对业务事件在调用方记录“未发送、发送中、已成功、结果未知”；
2. 收到 `200` 后保存 `id`、`msg_id` 和业务事件 ID 的对应关系；
3. 对 `401`、`409`、`422` 不自动重试；
4. 对明确的 `502` 使用有上限的指数退避，并先判断业务是否允许重复；
5. 对 `503 wechat_not_ready` 等待 `/health/ready` 恢复后再决定是否发送；
6. 对连接中断或客户端超时不要盲目重发，可转人工确认或发送带业务事件编号的
   补充通知。

接口可能包含微信请求和媒体上传，客户端不应使用过短超时。示例使用 90 秒，
调用方可根据文件大小和网络环境调整。

## 9. 当前能力边界

- 目标固定为唯一 owner；
- 不接受 `recipient`、`user_id`、联系人名称或 alias；
- 不提供外部收件人列表、聊天列表、按 ID 查询或微信状态接口；
- 不支持 Bot 主动发送原生引用；
- 不支持原生语音或视频发送；
- 图片和文件会整体 Base64 编码，请勿把本接口当作大文件传输通道；
- iLink 主动发送可能受互动窗口和限频约束，`ret=-2` 不能单独确定具体原因。

需要读取完整聊天、媒体或网关状态时使用受独立 Basic Auth 保护的 `/admin`。
需要接收微信入站消息时配置 HMAC Webhook，契约见
[HTTP 接口：入站 Webhook](api.md#7-入站-webhook)。

## 10. 上线检查清单

- [ ] `WBB_API_KEY` 使用高熵随机值且未写入仓库；
- [ ] API Key 与管理密码、Webhook 密钥互不相同；
- [ ] 跨主机流量经过 HTTPS 或受控私网；
- [ ] `/health/ready` 返回 200；
- [ ] owner 已主动向 Bot 发消息并完成绑定；
- [ ] 调用方能分别处理 401、409、422、502 和 503；
- [ ] 调用方理解每次 POST 都是新消息；
- [ ] 调用方保存成功响应并处理“结果未知”状态；
- [ ] Hook 内容已脱敏并设置长度上限；
- [ ] 图片和文件大小符合部署配置。
