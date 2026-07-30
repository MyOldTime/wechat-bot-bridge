# AGENT.md

本文件是 `wechat-bot-bridge` 仓库的维护约定，也是自动化开发代理的首要项目说明。修改代码前必须完整阅读本文件、`README.md` 和与任务相关的 `docs/` 文档。

## 项目事实

- 项目名：`wechat-bot-bridge`
- Python 包：`wechat_bot_bridge`
- Python：精确使用 `3.12.13`
- 依赖管理：uv；必须提交并遵守 `uv.lock`
- CLI：只保留 `wechat-bot-bridge serve`；微信扫码登录由 `/admin` 驱动
- 外部业务接口：仅 `POST /api/v1/messages`
- 管理页面：`/admin`，使用与外部 Bearer 分离的 Basic Auth
- 配置前缀：`WBB_`
- 源码分层：`api/`、`application/`、`bootstrap/`、`core/`、`domain/`、`infrastructure/`、`web/`
- 包目录使用 Python 3.12 隐式命名空间，不保留仅用于占位的 `__init__.py`
- Docker 持久化根目录：`/data`
- 凭据路径：`/data/wechat/credentials.json`
- 部署模型：单进程、单 Uvicorn worker、单 iLink 长轮询消费者

## 不可违反的能力边界

本项目基于微信 iLink Bot，不是桌面个人微信 Hook 或客户端注入。不要把功能、文档或测试描述成“监听全部个人微信会话”，也不要声称支持普通个人微信群。当前 ClawBot 是扫码账号与 Bot 之间的一对一私密通道；主动发送只能使用扫码账号先向 Bot 私聊后获得的唯一 owner 会话上下文，不得提供联系人列表、任意 `user_id` 目标或绕过该限制的伪接口。社区观察到主动互动窗口通常约为 24 小时，且窗口内仍有限频；上游 `ret=-2` 可能对应上下文过期或限频，不能硬编码成唯一原因。

`credentials.json` 是明文敏感授权文件。API Key、管理密码、页面令牌、Webhook secret、上游 token 和 `context_token` 不得进入 Git、测试快照、异常响应或日志。不要读取并输出真实 `data/` 内容。示例只能使用明显的假值。聊天正文和媒体只允许出现在 SQLite、微信/Webhook 业务载荷和已通过 Basic Auth 的管理端，不得写入普通日志。

## 架构约束

保持以下依赖方向：

```text
API/Web/CLI -> application services -> domain ports <- infrastructure adapters
```

- 路由只处理认证、校验和 HTTP 映射，不直接调用 iLink 原始 API。
- `api/external.py` 只提供 `POST /api/v1/messages`；健康检查放在 `api/health.py`，不创建聚合式 `router.py`。
- `web/` 管理页面必须保持服务端模板与同源静态资源结构；页面内部接口不得混入外部 OpenAPI。
- 应用服务编排唯一 owner 解析、消息创建、同步发送和审计状态迁移。
- `domain/` 不得导入 FastAPI、SQLite、HTTP 客户端或 iLink 协议模型；跨层依赖通过 `domain/ports.py`。
- `bootstrap/` 是唯一负责把具体存储、微信适配器、分发器和应用服务装配到一起的位置。
- `application/account.py` 编排管理端登录、退出和全量清理；账号发送与退出必须共享同一把操作锁。
- `application/payloads.py` 统一部署级载荷校验，不把这些规则重新塞回发送用例。
- API 的 Pydantic 请求/响应模型放在 `api/schemas.py`，不得混入领域模型。
- `infrastructure/persistence/sqlite.py` 只编排存取事务；建表迁移、行映射、分页游标和可取消安全事务分别放在 `schema.py`、`records.py`、`cursor.py`、`transactions.py`。
- `infrastructure/wechat/gateway.py` 负责授权/监听生命周期和领域错误映射；出站映射、入站处理、客户端契约和授权指纹分别放在 `delivery.py`、`inbound.py`、`protocols.py`、`session.py`，原始字典不得扩散到领域层。
- iLink 子模块按职责拆分：`auth.py` 管理二维码与兼容授权文件，`transport.py` 管理端点和脱敏错误，`messages.py` 管理 wire 消息，`crypto.py` / `media.py` 管理 AES/CDN，`client.py` 提供高层组合能力。不得重新合并为单个巨型适配器。
- `web/router.py` 只装配路由；页面、账号、消息和 WebSocket 处理分别留在 `web/routes/`，共享令牌/表单校验放在 `web/security.py`。
- 项目不得依赖或导入 `wechatbot-sdk`；协议行为以腾讯 `openclaw-weixin` 当前公开类型/API 和记录在 `docs/ilink-protocol.md` 的兼容约定为依据。更新协议前必须重新核对来源、补契约测试并同步文档。
- 出站当前只开放文字、图片、文件、正在输入和取消输入。不得宣称支持尚未建模和真实验证的原生语音、原生引用或视频发送，也不得用普通文件、拼接文本或页面私有字段伪装这些类型。管理页面和外部接口均不得接受出站引用字段。
- 入站消息先规范化，再交给 sink/dispatcher；新增下游时实现端口，不在监听循环中硬编码。
- 内置解析器生成的 `QuotedMessage` 是微信原生引用的入站快照，必须进入统一事件、SQLite、Webhook 和管理页面。官方入站 `WeixinMessage.message_id`、当前 `MessageItem.msg_id` 和引用目标 `MessageItem.msg_id` 进入领域前必须分别与账号 ID 一起哈希成隔离的内部关联键；SQLite 用引用目标键精确匹配原消息的顶层键或 item 键，命中后管理历史可以回填原文并返回目标消息 ID。不得按正文、时间或方向模糊猜测关联，也不得持久化原始协议 ID 或把关联键暴露到 Webhook。Bot 出站主消息只用内部消息 UUID 作为 iLink 顶层 `client_id`，不得伪造 item 的 `msg_id`；线上 `sendmessage` 成功响应的整数 `message_id` 与微信引用返回的 `MessageItem.msg_id` 相同，传输层必须解析该实际返回值，成功投递后再把账号隔离哈希写入对应出站聊天记录。响应缺少有效 ID 时不得猜测或用时间戳建立关联。
- 入站非文字媒体只通过内置 `MediaTransfer` 获取，必须同时有网络超时、流式密文上限和 `WBB_MEDIA_MAX_BYTES` 明文上限。内部媒体字段不得序列化到事件去重 JSON、日志或 Webhook；下载失败时仍保留消息类型，正文可以为空。
- iLink 的 `[image]`、`[voice]`、`[file]`、`[video]` 及中文等价占位符不得作为真实说明重复展示；解析器也不得把图片 URL 人为拼入正文。
- `ChatHistorySink` 是入站聊天落库和实时广播入口，必须排在日志与可选 Webhook 等后续 Sink 之前。
- WebSocket 是低延迟提示，不是持久事实来源；慢订阅者可以丢弃较早事件，客户端必须通过 SQLite 历史恢复并按消息 ID upsert。
- 长轮询中的入站处理保持串行、短小且有超时，不做无限重试；耗时下游处理不得阻塞长轮询。
- `get_updates_buf` 必须在整批消息处理成功后写入 SQLite；崩溃重放仍必须依靠持久化事件去重，不能假定 exactly-once。
- iLink token、`context_token`、二维码标识、带查询参数的 CDN URL 和原始响应正文不得进入异常文本或普通日志；低层错误只暴露操作名、HTTP 状态和数值错误码。
- 没有授权文件时常驻网关必须进入 `login_required` 等待管理页面，不得在容器终端申请或打印二维码。二维码原始 URL 只能短暂存在进程内，对页面只返回 Basic Auth 保护且 `no-store` 的二维码图片。
- iLink 要求配对数字时必须通过管理端异步提交，不得在常驻服务或容器中调用阻塞式 `input()`。
- 管理端退出必须先停止授权/长轮询生产者并排空 `InboundDispatcher`，再清空 SQLite，避免待处理入站事件在清理后重新写回。
- 持久层不得把 `context_token` 返回到 API 或 Webhook；除入站 Webhook 契约中的 `sender.user_id` 外，HTTP 业务响应不得暴露 owner 的 `user_id`。
- 所有后台任务都必须挂到应用 lifespan，支持 SIGTERM 和可预测清理。
- 保持一个长轮询所有者。没有分布式租约、共享队列和并发数据库设计前，不得启用多个 Uvicorn worker 或多副本。

完整说明见 `docs/architecture.md`。

## 开发工作流

只使用 uv 管理环境和依赖：

```bash
uv python install 3.12.13
uv sync --frozen
uv run pytest
uv run ruff check .
uv run ruff format --check .
uv run mypy src
```

新增或更新依赖时使用 `uv add` / `uv remove`，随后提交 `pyproject.toml` 与 `uv.lock`。不要用 `pip install` 手工改变项目环境，不要提交 `.venv`。国内网络可设置：

```text
UV_DEFAULT_INDEX=https://mirrors.aliyun.com/pypi/simple/
```

镜像只影响依赖获取，不应改变锁文件中的预期依赖集合。

## 实现规范

- 公开函数和跨层接口使用完整类型标注。
- 网络、文件、数据库等 I/O 使用明确超时和可分类异常。
- 密钥比较使用恒定时间比较；日志使用结构化、脱敏字段。
- 日志通过 `core/logging.py` 的安全白名单输出上下文。新增字段必须先确认不含正文、用户标识、URL 查询串、凭据或会话材料，不能依赖调用方“记得不要打印”。
- 不使用可变全局单例保存请求状态。
- SQLite 操作必须参数化；迁移要前向兼容并有测试。
- `chat_messages` 保存管理页面需要的完整内容、方向、来源、状态、账号隔离的顶层消息/item 哈希关联键和微信入站原生引用快照/精确目标；正文、媒体、关联键和 owner 上下文不得错误地进入外部 API。原始微信 `msg_id` 只允许在对应的受认证同步发送响应中返回，不得持久化、写日志、进入管理历史或 Webhook。
- `POST /api/v1/messages` 每次调用都创建不同消息；不得接收或依赖外部 `Idempotency-Key`。
- 消息 ID 由服务内部生成。调用方网络重试会形成新消息，API 和文档必须明确提示重复通知风险。
- 发送必须在当前 HTTP 请求中同步调用微信；成功返回 `200` 和终态 `sent`，可预期失败直接映射为稳定的 `4xx/5xx`，服务端不执行后台自动重试。
- 请求必须以 `type` 构成判别联合；当前允许 `text`、`image`、`file`、`typing_start` 和 `typing_cancel`。二进制只接受标准 Base64，并同时受 schema 硬上限和 `WBB_MEDIA_MAX_BYTES` 约束。
- 外部公开契约仅包含 `POST /api/v1/messages`；聊天查询、收件人和微信状态只属于管理端内部能力，不得暴露到 `/api/v1`。
- 管理历史只在 Basic Auth 下通过 `/admin/api/messages` 提供，使用不可变的 `occurred_at + id` 游标，不使用 offset。
- 管理历史可以返回完整正文和受保护媒体 URL，但不得返回 owner `user_id`、`context_token` 或任何授权材料。
- 管理页面键入触发的微信输入状态必须由浏览器定时器和服务端 `TypingLeaseService` 双重收口；正文发送和 lifespan 关闭前应尽力取消活动租约。
- 管理端与外部发送请求使用相同的消息载荷边界，均不得接受 `quote_msg_id`、`quote_message_id`、`ref_msg` 或其他引用字段。微信入站原生引用只作为只读内容进入历史与实时事件，精确匹配的目标消息 ID 仅用于管理页定位原文。
- 管理页面 HTTP 全部使用 Basic Auth；外部 Bearer 不得获得管理权限。JSON 写操作另需短期 `X-Admin-Token`；退出使用 URL 编码表单中的短期签名令牌；WebSocket 使用子协议头传递短期签名令牌，不得把令牌放入 URL。
- `POST /admin/logout` 必须清除授权文件、`chat_messages`、`inbound_events`、`outbound_messages`、`recipients`、`bridge_state` 和进程内页面队列，再启动新的扫码流程并 `303` 返回 `/admin`。
- `WBB_WEB_PASSWORD` 未配置时管理端必须显式关闭，不能回退为无认证模式。
- HTTP 发送目标固定为 `owner`，请求体不得接收 `recipient`、`user_id` 或 alias。
- 只有与当前授权凭据 `user_id` 完全一致的入站发送者可以建立或刷新 owner；授权会话改变时必须清空上一会话的 owner。
- 健康接口不鉴权但只返回最小状态；所有 `/api/v1` 业务接口必须 Bearer 鉴权。
- 不把 iLink wire 类型作为公共 API 模型；外部契约必须版本化。

## 测试要求

修改应在相应层补测试：

- 配置：默认值、`WBB_` 覆盖、非法/空 secret。
- 认证：缺失、错误、正确 Bearer 与 Basic；确保恒定时间路径、两套凭据隔离和管理端禁用状态可覆盖。
- 发送：owner 未绑定、缺失上下文、成功返回微信 `msg_id`、输入状态返回空 `msg_id`、上游失败。
- 独立提交：相同正文的顺序或并发请求都必须创建不同消息。
- 查询：管理聊天正文、媒体保护、游标连续翻页、非法游标，以及 `/api/v1` 非公开路径不可访问。
- 消息类型：文字、图片、文件、输入开始/取消到 iLink wire 消息的映射；原生语音、原生引用和视频必须被拒绝，直到内置客户端完成稳定建模和真实协议验证；管理端和外部接口携带引用字段时必须拒绝。
- 入站：规范化、原生引用快照、引用 `MessageItem.msg_id` 对入站 `WeixinMessage.message_id`/`MessageItem.msg_id` 和出站 `sendmessage.message_id` 的精确关联、缺失原文回填、内部协议 ID 不进入 Webhook、媒体占位符过滤、owner 上下文更新、非扫码账号不能抢占 owner、CDN 解密、超时/大小限制和媒体不进入 Webhook。
- 实时页面：外部出站 `sending -> sent/failed`、微信入站 `received`、按 ID upsert 和无效 WebSocket 令牌。
- 页面交互：键入启动/续租/超时取消、微信原生引用快照、点击已匹配引用定位并闪高两次、入站图片预览和文件下载；不得重新引入右键引用或出站引用预览。
- Sink：`ChatHistorySink` 的落库与广播、慢订阅者队列行为。
- iLink 客户端：必需请求头、`ret/errcode` 映射、`sendmessage.message_id` 解析、长轮询超时、cursor 恢复、二维码/配对码/IDC 跳转、授权文件格式、AES 三种 key 编码、CDN 上限、重启去重和账号切换后 context 作废。
- Webhook：签名、超时、重试且不泄漏上下文。
- 生命周期：启动、SIGTERM/关闭、轮询任务失败。
- 账号生命周期：无凭据时等待页面、登录启动、二维码保护、配对数字、退出表单认证、凭据删除、队列排空、全表清理、并发发送互斥和页面 `account_reset`。
- API/Web：外部 OpenAPI 只有一个 POST；管理页面、静态资源、CSP、状态码和响应模型。

测试目录应镜像源码职责，分别放在 `tests/api/`、`tests/application/`、`tests/core/`、`tests/infrastructure/` 和 `tests/web/`。测试不得访问真实微信、真实凭据或公网。通过端口/适配器注入 fake。涉及 SQLite 的测试使用独立临时目录。

## Docker 约束

- 基础镜像固定 Python `3.12.13` slim，uv 固定 `0.11.32`。
- 构建参数 `UV_DEFAULT_INDEX` 默认阿里云镜像并允许覆盖。
- 运行用户保持 UID/GID `10001` 的非 root 用户。
- `./data:/data` 必须保留，授权文件明确落到 `/data/wechat/credentials.json`。
- API 默认只发布到宿主机 `127.0.0.1:8000`。
- 启动命令保持一个 Uvicorn worker；改为多进程或多副本前必须先完成架构中的协调设计。
- 健康检查使用 `/health/live`。

修改 Docker 行为后至少执行：

```bash
docker compose config
docker compose build
docker compose run --rm wechat-bot-bridge --help
```

CLI 帮助中只能出现常驻 `serve`，扫码登录统一由管理页面处理。

如果当前环境不能构建，应在交付说明中明确未验证项，不能声称已通过。

## 文档同步矩阵

以下修改不能只改代码：

| 修改 | 必须同步 |
| --- | --- |
| 环境变量或默认值 | `.env.example`、`README.md` |
| 外部 API 请求/响应/状态码 | `docs/external-api.md`、`docs/api.md`、相关测试 |
| 管理页面、WebSocket 请求/响应/状态码 | `docs/api.md`、相关测试 |
| 持久化路径或 schema | `docs/architecture.md`、Docker/Compose、迁移测试 |
| CLI | `README.md`、Dockerfile、Compose 示例 |
| 入站信封或签名 | `docs/api.md`、`docs/architecture.md` |
| 页面模板、静态资源或认证 | `README.md`、`docs/api.md`、浏览器验收 |
| 登录、退出或本地清理语义 | `README.md`、`docs/api.md`、`docs/architecture.md`、生命周期测试 |
| 架构边界 | `AGENT.md`、`docs/architecture.md` |

## 完成定义

交付前：

1. 检查 `git diff`，不要覆盖用户无关修改。
2. 运行格式、静态检查和测试。
3. 对 Docker/Compose 变更运行配置校验与构建（环境允许时）。
4. 确认没有真实密钥、凭据、数据库或个人消息进入 diff。
5. 确认 README、外部接口、管理接口和架构文档与实际行为一致。
6. 在交付说明中列出已执行验证和任何已知限制。
