# iLink 协议实现说明

## 1. 目标

项目直接实现微信 iLink Bot 所需的最小协议，不安装或导入
`wechatbot-sdk`。授权文件、长轮询 cursor、owner 上下文、媒体大小限制和
错误脱敏均由本仓库测试和维护，iLink 原始 JSON 不扩散到领域、HTTP API
或管理页面。

该实现仍然是 iLink Bot 客户端，不是桌面微信 Hook。当前产品能力边界没有
因此扩大：通道仍是扫码账号与 Bot 的一对一私聊。

## 2. 参考来源

协议实现依据：

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

## 4. iLink 端点

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

## 5. 授权文件与登录

授权文件路径是 `/data/wechat/credentials.json`，使用以下 camelCase 字段：

```json
{
  "token": "<secret>",
  "baseUrl": "https://...",
  "accountId": "...",
  "userId": "...",
  "savedAt": "..."
}
```

保存采用 camelCase 格式和临时文件原子替换。二维码流程支持 `wait`、
`scaned`、`confirmed`、`expired`、
`need_verifycode`、`verify_code_blocked`、`scaned_but_redirect` 和
`binded_redirect`。

授权流程由常驻网关和 Basic Auth 管理页面驱动：

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

- 只把 `message_type=USER` 的消息交给业务层，忽略 `message_type=BOT`；
- 优先使用 `message_id`，再使用 `client_id`、`seq` 作为上游稳定 ID；
- 极端情况下缺少全部 ID 时，对原始 JSON 做规范化 SHA-256；
- 媒体类型由 `MessageItem.type` 决定，即使 CDN 引用缺失也不会降级成文字；
- 正文只抽取真实 `text_item.text` 和语音转写，不生成 `[image]`，也不拼接
  图片 URL；
- 当前入站消息的顶层 `WeixinMessage.message_id`、当前 `MessageItem.msg_id` 与 `ref_msg.message_item.msg_id` 分别和账号 ID 一起哈希成隔离的内部关联键；微信引用微信消息时，引用 item ID 可以对应原消息的顶层 message ID，因此 SQLite 用引用键精确匹配原消息的顶层键或 item 键。命中后才回填引用原文并建立管理页目标，不按正文或时间猜测，也不持久化原始协议 ID；
- iLink 成功响应包含整数 `message_id`，而且它与微信引用该 Bot 消息时返回的
  `ref_msg.message_item.msg_id` 完全相同。Bot 出站主消息使用内部消息 UUID
  作为顶层 `client_id`，不伪造 item 的 `msg_id`；传输层解析实际响应，
  只保存 `(account_id, message_id)` 的哈希。item `msg_id` 由服务端分配，
  客户端值不能作为引用关联依据；
- `ref_msg` 转换成一层 `QuotedMessage` 快照；顶层 message ID 和协议 item ID 都不进入去重 JSON、Webhook 或普通日志；
- `ref_msg` 只作为微信入站结构解析；公共发送请求不接受 Bot 出站引用字段；
- 新 `get_updates_buf` 只在本批消息全部交给入站处理后写入 SQLite；
- 重启重放由稳定 `event_id` 和 `inbound_events` 去重，不承诺 exactly-once。

发送文字按不超过 4000 字符的自然边界切片。首段 `client_id` 使用内部消息 UUID，后续
分段使用带序号的唯一 ID。图片或文件说明先用带 `:caption` 后缀的 ID 作为
文字 `sendmessage` 发送，媒体主项使用内部消息 UUID；因此说明成功、媒体
失败时可能出现部分提交，调用方仍会收到媒体发送失败。服务只把主项关联到
一条本地聊天记录。管理页面和外部接口都不提供出站引用字段；`ref_msg`
仅在微信入站消息中解析并保存快照。

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
key 的图片按腾讯官方实现作为 CDN 明文处理，其他无 key 媒体拒绝处理。

## 8. 错误与重试

- `ret != 0` 或 `errcode != 0` 转换为只含操作名、HTTP 状态和错误码的
  `ILinkApiError`；
- `errcode=-14` 触发清除失效授权并回到 `login_required`，由管理页面重新扫码；
- `ret=-2` 在出站路径保守映射为上下文过期或限频，不假定唯一原因；
- 长轮询读超时是正常空批次，连接、DNS、TLS 等失败进入指数退避；
- CDN 上传只对网络错误和 5xx 做最多三次尝试，4xx 立即失败；
- 出站 `sendmessage` 不自动重试，避免产生重复通知。

## 9. 协议变更检查

升级协议实现前必须：

1. 记录核对的腾讯提交或版本；
2. 比较端点、认证头、`BaseInfo`、错误码和请求超时；
3. 比较 `WeixinMessage`、`MessageItem`、引用和 CDN 字段；
4. 验证授权文件格式和敏感文件权限；
5. 更新 transport/messages/media/auth 的离线契约测试；
6. 使用测试账号人工验证扫码、入站文字/图片/文件/引用、出站文字/图片/
   文件和输入状态；
7. 同步 `AGENT.md`、README 与架构文档。
