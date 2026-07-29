# iLink 协议实现说明

## 1. 目标

项目直接实现微信 iLink Bot 所需的最小协议，不再安装或导入
`wechatbot-sdk`。这样可以把授权文件、长轮询 cursor、owner 上下文、媒体
大小限制和错误脱敏全部纳入本仓库的测试与维护边界，同时不让 iLink 原始
JSON 扩散到领域、HTTP API 或管理页面。

该实现仍然是 iLink Bot 客户端，不是桌面微信 Hook。当前产品能力边界没有
因此扩大：通道仍是扫码账号与 Bot 的一对一私聊。

## 2. 参考来源

本次实现于 2026-07-29 核对：

- 腾讯官方实现
  [`Tencent/openclaw-weixin`](https://github.com/Tencent/openclaw-weixin)，
  核对提交 `cef0bfc390393f716903e16d50408118047f87e0`；
- 腾讯公开的
  [`src/api/types.ts`](https://github.com/Tencent/openclaw-weixin/blob/main/src/api/types.ts)
  和
  [`src/api/api.ts`](https://github.com/Tencent/openclaw-weixin/blob/main/src/api/api.ts)，
  以及把结构化 `MessageItem` 原样放入 `sendmessage.item_list` 的
  [`src/messaging/send.ts`](https://github.com/Tencent/openclaw-weixin/blob/main/src/messaging/send.ts)；
- Python 行为参考
  [`corespeed-io/wechatbot/python`](https://github.com/corespeed-io/wechatbot/tree/main/python)，
  核对提交 `6914de4e4cae966b1a3f4c6531711c9dffd84fee`。

两个参考仓库均以 MIT 许可证发布。本项目没有把它们作为运行依赖；实现按
本项目的分层、安全约束和测试重新组织。

## 3. 模块边界

```text
infrastructure/wechat/
├── gateway.py              授权状态机、监听生命周期、cursor、错误映射
├── inbound.py              owner 校验、规范化、去重、媒体和分发
├── delivery.py             领域出站载荷映射
├── protocols.py            网关所需的最小客户端契约
├── session.py              授权会话指纹
└── ilink/
    ├── auth.py             授权文件、二维码、配对码、IDC 跳转
    ├── client.py           高层 iLink 操作
    ├── transport.py        HTTP、认证头、超时、API/CDN 错误
    ├── messages.py         wire 消息构造、切片、入站解析
    ├── models.py           私有协议模型
    ├── crypto.py           AES-128-ECB 与 key 编码
    ├── media.py            CDN 上传、流式下载、解密、MIME
    ├── constants.py
    └── errors.py
```

依赖方向是：

```text
gateway -> inbound / delivery / session / client
                                  client -> messages / media -> transport
                                                      media -> crypto
                                  auth ---------------------> transport
```

只有 `transport.py` 发起 iLink/CDN HTTP 请求；只有 `messages.py` 解释
`getupdates` 原始消息字典。

## 4. 已实现端点

| 操作 | iLink 路径 | 用途 |
| --- | --- | --- |
| 二维码申请 | `/ilink/bot/get_bot_qrcode?bot_type=3` | 获取登录二维码 |
| 二维码状态 | `/ilink/bot/get_qrcode_status` | 等待扫码、配对码、确认或跳转 |
| 长轮询 | `/ilink/bot/getupdates` | 获取新消息和下一 cursor |
| 发送消息 | `/ilink/bot/sendmessage` | 发送文字或已上传媒体引用 |
| 上传参数 | `/ilink/bot/getuploadurl` | 获取 CDN 上传参数或完整 URL |
| 输入配置 | `/ilink/bot/getconfig` | 获取 `typing_ticket` |
| 输入状态 | `/ilink/bot/sendtyping` | `1` 开始、`2` 取消 |
| 启停通知 | `/ilink/bot/msg/notifystart`、`notifystop` | 客户端生命周期提示 |

认证请求包含：

```text
Content-Type: application/json
AuthorizationType: ilink_bot_token
Authorization: Bearer <token>
X-WECHAT-UIN: <随机 uint32 十进制字符串的 Base64>
iLink-App-Id: bot
iLink-App-ClientVersion: <0x00MMNNPP 的十进制表示>
```

每个 CGI 请求体还携带 `base_info.channel_version` 与
`base_info.bot_agent`。token、请求体、完整响应和带查询参数的 CDN URL
不会写入普通日志。

## 5. 授权兼容

默认授权文件仍是 `/data/wechat/credentials.json`。读取同时兼容旧版
Python SDK 使用的字段：

```json
{
  "token": "<secret>",
  "baseUrl": "https://...",
  "accountId": "...",
  "userId": "...",
  "savedAt": "..."
}
```

保存采用同一 camelCase 格式和临时文件原子替换，因此升级本项目不要求
重新扫码。二维码流程支持 `wait`、`scaned`、`confirmed`、`expired`、
`need_verifycode`、`verify_code_blocked`、`scaned_but_redirect` 和
`binded_redirect`。

授权流程由常驻网关和 Basic Auth 管理页面驱动，不再提供交互式 CLI：

- 没有凭据时网关停在 `login_required`，直到页面明确请求登录；
- `qrcode_img_content` 只短暂保存在进程内，Web 层编码为受保护的 SVG，
  不打印到终端、不写日志，也不作为原始字符串返回；
- `need_verifycode` 通过异步回调等待管理页面提交配对数字，不会在容器中
  调用 `input()`；
- 登录取消与授权文件保存采用可取消但有序的文件操作，保证退出清除不会
  被仍在后台完成的保存线程反向覆盖。

授权文件中的 API base URL、服务端跳转地址和 CDN 地址必须使用 HTTPS，
且不能包含 URL 用户名或密码。

## 6. 消息和 cursor

- 只把 `message_type=USER` 的消息交给业务层，忽略 Bot 回显；
- 优先使用 `message_id`，再使用 `client_id`、`seq` 作为上游稳定 ID；
- 极端情况下缺少全部 ID 时，对原始 JSON 做规范化 SHA-256；
- 媒体类型由 `MessageItem.type` 决定，即使 CDN 引用缺失也不会降级成文字；
- 正文只抽取真实 `text_item.text` 和语音转写，不生成 `[image]`，也不拼接
  图片 URL；
- `ref_msg` 转换成一层 `QuotedMessage` 快照，不伪造历史目标 ID；
- `ref_msg` 虽位于收发共享的 `MessageItem` 类型中，但腾讯公开发送能力只
  列出文字、图片、视频和文件，官方发送实现也不构造出站引用。2026-07-29
  使用真实微信验证时，服务端接受含 `ref_msg` 的请求却只投递正文，因此
  当前只把它作为入站结构，不宣称支持 Bot 出站原生引用；
- 新 `get_updates_buf` 只在本批消息全部交给入站处理后写入 SQLite；
- 重启重放由稳定 `event_id` 和 `inbound_events` 去重，不承诺 exactly-once。

发送文字按不超过 4000 字符的自然边界切片。图片或文件说明先作为文字
`sendmessage` 发送，再上传和发送媒体项；因此说明成功、媒体失败时可能
出现部分提交，调用方仍会收到媒体发送失败。管理端引用使用明确标记的普通
文本降级，并在 SQLite 中保留结构化关系。

## 7. CDN 与 AES

出站流程：

1. 生成随机 16 字节 AES key 和 file key；
2. 计算明文 MD5、明文长度和 PKCS#7 填充后的密文长度；
3. 调用 `getuploadurl`，优先使用 `upload_full_url`，否则安全拼接
   `upload_param`；
4. 使用 AES-128-ECB 加密，向 CDN POST 密文；
5. 读取 `x-encrypted-param`，把 CDN 引用和 Base64(hex key) 放入消息项。

入站兼容三种已观察到的 key 表示：

- 32 字符直接 hex；
- Base64(raw 16 bytes)；
- Base64(32 字符 hex string)。

下载先检查 `Content-Length`，再逐块累计密文字节；超过
`WBB_MEDIA_MAX_BYTES + 16` 立即终止。解密后再次检查明文上限。少量不带
key 的历史图片按腾讯官方实现兼容为 CDN 明文，其他无 key 媒体拒绝处理。

## 8. 错误与重试

- `ret != 0` 或 `errcode != 0` 转换为只含操作名、HTTP 状态和错误码的
  `ILinkApiError`；
- `errcode=-14` 触发清除失效授权并回到 `login_required`，由管理页面重新扫码；
- `ret=-2` 在出站路径保守映射为上下文过期或限频，不假定唯一原因；
- 长轮询读超时是正常空批次，连接、DNS、TLS 等失败进入指数退避；
- CDN 上传只对网络错误和 5xx 做最多三次尝试，4xx 立即失败；
- 出站 `sendmessage` 不自动重试，避免产生重复通知。

## 9. 升级审计

升级协议实现前必须：

1. 记录核对的腾讯提交或版本；
2. 比较端点、认证头、`BaseInfo`、错误码和请求超时；
3. 比较 `WeixinMessage`、`MessageItem`、引用和 CDN 字段；
4. 验证旧授权文件仍能读取；
5. 更新 transport/messages/media/auth 的离线契约测试；
6. 使用测试账号人工验证扫码、入站文字/图片/文件/引用、出站文字/图片/
   文件和输入状态；
7. 同步 `AGENT.md`、README 与架构文档。
