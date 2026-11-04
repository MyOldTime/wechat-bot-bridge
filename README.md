# wechat-bot-bridge

`wechat-bot-bridge` 是一个使用 Python 3.12.13 与 uv 管理的微信 iLink Bot 本地桥接服务。自动化任务通过一个稳定的 HTTP 接口向我的微信发送通知；服务同时监听微信入站消息，将完整聊天记录保存到 SQLite，并在同源管理页面实时展示。

> [!IMPORTANT]
> 本项目接入的是微信 **iLink Bot**，不是桌面个人微信 Hook、注入或抓取工具。当前通道是扫码账号与 Bot 之间的一对一私聊，不能监听该账号的全部好友、普通微信群或其他私人会话。扫码账号必须先向 Bot 发一条消息，服务取得唯一 `owner` 的会话上下文后，外部系统才能主动通知。

## 能力

- 外部系统只有一个业务接口：`POST /api/v1/messages`。
- 支持文字、图片、普通文件、开始输入和取消输入；每次 POST 都是独立消息。
- 外部文字、图片和文件发送成功时同时返回内部 `id` 与微信服务端 `msg_id`。
- 外部 API 使用管理页面自动生成的 Bearer Token，管理页面使用独立的 HTTP Basic Auth。
- `/admin` 提供非前后端分离的服务端页面，可查看完整聊天内容并发送消息。
- 微信扫码授权统一在受 Basic Auth 保护的 `/admin` 页面完成。
- 管理页面可退出微信；退出会停止监听并清除授权文件、全部聊天/媒体、owner、游标、入站去重和出站审计，然后返回扫码界面。
- 管理页面会随输入框键入自动同步“正在输入”，并由浏览器定时器和服务端租约双重兜底取消。
- 微信发来的原生引用会保留标题与原文快照；上游只回传引用 item ID 时，会精确匹配已保存历史并回填原文。匹配成功的引用框可以点击定位，原消息会闪高两次。
- 管理页面和外部接口都不提供出站引用；页面只读展示微信发来的原生引用。
- 微信入站图片和文件会通过内置 iLink 客户端下载、解密后落入 SQLite，并在管理页面以内嵌预览或受保护下载链接展示。
- 外部消息、页面消息和微信入站消息通过 WebSocket 及时更新到页面。
- `/admin` 左侧可以查看、复制和轮换公共 API Token，并配置单向 HTTP 消息转发。
- `ChatHistorySink` 将微信消息保存到 SQLite；`OutboundForwardingSink` 可按页面配置使用 HMAC 并行转发规范化事件。
- Docker Compose 保持单进程部署，并把数据库和微信授权文件映射到宿主机 `./data`。
- Windows 可生成不依赖 Python/uv 的单文件 `wechat-bot-bridge.exe`；页面资源嵌入 EXE，数据库和微信授权始终保存在 EXE 外部。

外部系统接入见 [外部消息接口对接指南](docs/external-api.md)，管理端与
单向转发契约见 [HTTP 接口](docs/api.md)，设计和边界见
[架构文档](docs/architecture.md)。
Windows 原生部署见 [Windows EXE 部署](docs/windows-exe.md)。

## 项目结构

源码按职责分层，页面模板和静态资源也归入独立 Web 边界：

```text
src/wechat_bot_bridge/
├── api/
│   ├── external.py            # 外部唯一 POST 路由
│   ├── health.py              # 最小健康检查
│   ├── security.py            # Bearer 认证
│   ├── errors.py              # HTTP 错误映射
│   └── schemas.py             # 外部请求/响应模型
├── application/
│   ├── account.py             # 登录控制、退出与全量清理
│   ├── configuration.py       # 公共 Token 与动态转发配置
│   ├── messaging.py           # 同步发送编排
│   ├── payloads.py            # 统一载荷校验
│   └── typing.py              # 管理页面输入状态租约
├── bootstrap/
│   ├── app.py                 # FastAPI 生命周期与依赖装配
│   ├── cli.py                 # serve
│   └── single_instance.py     # 部署数据目录单实例锁
├── core/
│   ├── config.py              # WBB_* 配置
│   ├── paths.py               # 源码/冻结程序路径策略
│   └── logging.py             # 安全结构化日志
├── domain/
│   ├── models.py              # 领域模型
│   ├── ports.py               # 可替换端口
│   └── errors.py              # 稳定业务错误
├── infrastructure/
│   ├── inbound/
│   │   ├── dispatcher.py      # 入站 Sink 扇出
│   │   └── chat_history.py    # SQLite 聊天记录 Sink
│   ├── persistence/
│   │   ├── sqlite.py          # 事务与存取编排
│   │   ├── schema.py          # Schema、迁移与启动恢复
│   │   ├── records.py         # SQLite 行到领域模型的转换
│   │   ├── cursor.py          # 版本化历史游标
│   │   └── transactions.py    # 可取消安全事务边界
│   ├── realtime/
│   │   └── conversation.py    # WebSocket 进程内事件总线
│   └── wechat/
│       ├── gateway.py           # 授权与监听生命周期、领域错误映射
│       ├── inbound.py           # owner 校验、规范化、去重与媒体处理
│       ├── delivery.py          # 出站领域载荷映射
│       ├── protocols.py         # 网关最小客户端契约
│       ├── session.py           # 授权会话指纹
│       └── ilink/
│           ├── auth.py          # 二维码、配对码与授权文件
│           ├── client.py        # 内置 iLink 高层客户端
│           ├── transport.py     # HTTP 端点、认证头与脱敏错误
│           ├── messages.py      # wire 消息构造与入站解析
│           ├── models.py        # 私有协议模型
│           ├── crypto.py        # AES-128-ECB
│           ├── media.py         # CDN 上传、流式下载与解密
│           ├── constants.py
│           └── errors.py
└── web/
    ├── router.py               # 管理路由装配
    ├── routes/
    │   ├── page.py             # HTML 页面
    │   ├── account.py          # 登录、状态与退出
    │   ├── messages.py         # 历史、媒体与发送
    │   ├── configuration.py    # Token 与转发管理接口
    │   └── realtime.py         # WebSocket 事件
    ├── schemas.py
    ├── security.py             # Basic Auth 与短期页面令牌
    ├── qr.py                   # 二维码 SVG 渲染
    ├── templates/
    │   └── chat.html
    └── static/
        ├── chat.css
        └── chat.js
```

包目录使用 Python 3.12 隐式命名空间，不保留无行为的 `__init__.py`。依赖方向是 `api/web/bootstrap -> application -> domain <- infrastructure`；iLink 原始字典和授权细节不会扩散到路由或领域层。项目直接实现所需的 iLink 协议，不依赖 `wechatbot-sdk`，协议依据见 [iLink 协议实现说明](docs/ilink-protocol.md)。

## Docker Compose 快速开始

要求 Docker Engine 与 Docker Compose v2。

```bash
cp .env.example .env
mkdir -p data/wechat
```

PowerShell：

```powershell
Copy-Item .env.example .env
New-Item -ItemType Directory -Force data/wechat
```

编辑 `.env`，必须设置管理页面密码：

```dotenv
WBB_WEB_PASSWORD=<管理页面密码，至少 12 字符>
```

Linux 宿主机还需让容器内的非 root 用户（UID/GID `10001`）可写数据目录：

```bash
sudo chown -R 10001:10001 ./data
chmod 700 ./data ./data/wechat
```

构建并启动服务：

```bash
docker compose up -d --build
docker compose ps
curl http://127.0.0.1:8000/health/live
```

打开 [http://127.0.0.1:8000/admin](http://127.0.0.1:8000/admin)，输入 `.env` 中的 `WBB_WEB_USERNAME` / `WBB_WEB_PASSWORD`。没有授权文件时，页面会自动申请并展示微信二维码；扫码并在微信确认即可。若微信要求配对数字，直接在同一页面填写。

授权成功后，文件会写到容器 `/data/wechat/credentials.json`，通过 Compose 映射后可在宿主机看到：

```text
./data/
├── wechat-bridge.db
└── wechat/
    └── credentials.json
```

`credentials.json` 含明文敏感授权材料。限制目录权限、保护备份、不要提交 Git，也不要把文件内容发到日志、聊天或工单。

随后使用完成扫码的同一微信账号向 Bot 发一条消息。登录只证明账号已授权，
并不会自动产生主动发送所需的聊天上下文；这条入站消息会建立唯一 `owner`。
`/health/ready` 只检查服务和微信监听是否就绪，不检查 owner 是否已建立；
owner 缺失时发送接口会返回 `409 owner_not_bound`。

公共 API Token 会在首次启动时自动生成。进入 `/admin` 后，点击左侧
“公共 API Token”菜单即可查看和复制，不需要也不支持通过 `.env` 配置。

## 使用外部通知接口

文字通知可以直接调用：

```bash
curl -X POST http://127.0.0.1:8000/api/v1/messages \
  -H "Authorization: Bearer <从管理页面复制的 Token>" \
  -H "Content-Type: application/json" \
  -d '{"type":"text","text":"自动化任务已完成"}'
```

接口还支持图片、普通文件、开始输入和取消输入，目标固定为唯一 `owner`。
发送在当前 HTTP 请求内同步完成；每次 POST 都代表一条新消息，网络超时后的
重复调用可能产生重复通知。完整请求模型、Python/Hook 示例、响应字段、错误码
和重试规则见 [外部消息接口对接指南](docs/external-api.md)。

## 使用管理页面

打开 [http://127.0.0.1:8000/admin](http://127.0.0.1:8000/admin)，浏览器会请求 Basic Auth：

- 用户名：`WBB_WEB_USERNAME`，默认 `admin`
- 密码：`WBB_WEB_PASSWORD`

页面可以：

- 在首次启动、授权过期或退出后展示二维码，并在需要时提交微信配对数字；
- 查看 SQLite 中保存的完整文字、图片和文件消息；微信入站图片可点开原图，文件可下载；
- 区分微信入站、外部任务和管理页面发送来源；
- 展示 `sending`、`sent`、`failed`、`received` 状态；
- 直接发送文字、图片或文件；
- 在输入框键入时自动向微信同步“正在输入”，停止输入后自动取消；
- 将微信发来的原生引用按只读快照展示；精确匹配到本地入站或 Bot 出站历史时可点击引用框定位原文并闪高两次，不提供右键引用或 Bot 主动引用；
- 通过 WebSocket 接收实时更新，断线后自动重连，并可通过历史接口恢复；
- 查看、复制和轮换公共 API Token；轮换后旧 Token 立即失效；
- 配置单向消息转发的协议、地址、端口、路径、HMAC Secret、超时和最大尝试次数；
- 通过原生 HTML 表单退出登录；该请求同时要求 Basic Auth 和短期页面令牌，成功后清除全部本地账号数据并回到扫码界面。

原生引用属于微信入站能力。服务保存入站顶层 `WeixinMessage.message_id`、
当前 `MessageItem.msg_id` 和出站 `sendmessage.message_id` 的账号隔离哈希，
再用引用中的 `MessageItem.msg_id` 精确关联原消息。命中后可以回填上游省略
的正文，点击引用框会定位并闪高原消息；没有精确目标时仍展示上游快照，但
不猜测目标。管理页面和外部接口均不接受 Bot 出站引用字段。

管理页面的 Basic 凭据与外部 Bearer Token 相互独立。登录管理页面后，Token 和转发配置操作不再重复要求密码。没有配置 `WBB_WEB_PASSWORD` 时，`/admin` 返回 `503 web_admin_disabled`。生产环境跨主机访问时必须放在 HTTPS 反向代理或私有网络后，不要把 Basic Auth 明文暴露在不可信网络。

退出是不可撤销的账号数据清理操作：SQLite 五张业务表中的聊天、媒体、owner、cursor、去重和审计记录都会删除，`credentials.json` 也会删除。公共 API Token 和单向转发配置属于服务级配置，会保留不变。页面会广播账号重置事件，使其他已打开的管理页面丢弃缓存视图；宿主机映射的数据库文件和目录本身会保留，以便立即重新登录。

## 微信入站与统一处理

内置客户端通过 `getupdates` 收到用户消息后，会校验发送者是扫码账号、更新 `owner` 上下文、规范化正文和原生引用并持久去重。入站的顶层 `WeixinMessage.message_id`、当前及引用目标的 `MessageItem.msg_id`，以及出站成功响应的 `message_id` 都会与账号 ID 一起哈希成内部关联键。SQLite 以引用 item 键精确匹配原消息的顶层键或 item 键，命中时回填缺失原文并生成管理页定位关系；不做正文或时间模糊匹配，原始协议 ID 和关联键也不会进入 Webhook。解析器不会人为生成 `[image]` 等媒体占位符或把图片 URL 拼入正文；图片、文件、语音或视频在超时和流式大小限制内从 CDN 下载并使用 AES-128-ECB 解密。成功下载的字节只交给聊天历史，不进入 Webhook 信封。`InboundDispatcher` 按顺序调用：

1. `ChatHistorySink`：写入 `chat_messages` 并广播 WebSocket；
2. `LoggingSink`：只记录脱敏元数据；
3. `OutboundForwardingSink`：启用时按当前页面配置使用独立 HMAC Secret 转发。

进入 `/admin` 左侧“消息转发”页面配置目标。默认关闭，保存后从下一条微信
入站事件开始生效，无需重启。该功能只主动发送，不接收下游执行结果；下游
需要通知微信时仍调用公共 `POST /api/v1/messages`。

单向转发使用有界进程内队列和有限重试，不提供重启续投。需要可靠消费时，
下游应快速落盘并按 `event_id` 去重。

## 本地开发

```bash
uv python install 3.12.13
uv sync --frozen
uv run wechat-bot-bridge serve
```

然后访问 `/admin` 完成扫码。CLI 提供常驻 `serve`，授权由管理页面处理。

项目的 uv 索引默认配置为阿里云 PyPI 镜像，也可显式覆盖：

```powershell
$env:UV_DEFAULT_INDEX = "https://mirrors.aliyun.com/pypi/simple/"
uv sync --frozen
```

验证：

```bash
uv run pytest
uv run ruff check .
uv run ruff format --check .
uv run mypy src
uv build
```

## Windows 单文件 EXE

在 Windows x64 开发机运行：

```powershell
.\scripts\build-windows.ps1
.\scripts\test-windows-release.ps1 -SkipBuild
```

脚本使用 `build/uv-pyinstaller` 独立环境，不会改动正在运行 Bridge 的项目 `.venv`。产物：

```text
dist/wechat-bot-bridge-windows-x64/
├── wechat-bot-bridge.exe
├── windows.env.example
├── README-WINDOWS.txt
├── LICENSE
└── SHA256SUMS.txt
dist/wechat-bot-bridge-windows-x64.zip
```

复制 `windows.env.example` 为 EXE 同目录的 `.env`，设置独立的
`WBB_WEB_PASSWORD` 后，可以双击 EXE；无参数启动会默认执行 `serve`。也可以在终端运行：

```powershell
.\wechat-bot-bridge.exe serve
```

EXE 同目录只放程序和 `.env`。默认持久数据位于：

```text
%LOCALAPPDATA%\wechat-bot-bridge\
├── wechat-bridge.db
├── wechat-bot-bridge.lock
└── wechat\credentials.json
```

单文件启动器会把只读代码和页面资源释放到临时目录，但 SQLite、锁文件和微信授权不会进入
临时目录或 EXE。一个持久化目录只允许一个 Bridge 进程。完整安装、迁移、升级与排障见
[Windows EXE 部署](docs/windows-exe.md)。Docker 和 uv 启动方式保持不变。

## 配置

| 变量 | Docker 默认值 | 说明 |
| --- | --- | --- |
| `WBB_WEB_USERNAME` | `admin` | 管理页面 Basic Auth 用户名 |
| `WBB_WEB_PASSWORD` | 未设置 | 管理页面独立密码；未设置时页面关闭，至少 12 字符 |
| `WBB_WEB_TOKEN_TTL_SECONDS` | `3600` | 页面写操作和 WebSocket 短期令牌有效期 |
| `WBB_WEB_TYPING_TIMEOUT_SECONDS` | `5` | 页面输入状态服务端租约秒数，超时后自动取消 |
| `WBB_HOST` | `0.0.0.0` | 容器内监听地址 |
| `WBB_PORT` | `8000` | 容器内监听端口 |
| `WBB_LOG_LEVEL` | `info` | 日志级别 |
| `WBB_DATA_DIR` | `/data` | 持久化根目录 |
| `WBB_DATABASE_PATH` | `/data/wechat-bridge.db` | SQLite 路径 |
| `WBB_CREDENTIALS_PATH` | `/data/wechat/credentials.json` | 明文 iLink 授权文件 |
| `WBB_INBOUND_QUEUE_SIZE` | `1000` | 入站分发队列容量 |
| `WBB_INBOUND_MEDIA_DOWNLOAD_TIMEOUT_SECONDS` | `30` | 微信入站媒体单次下载超时秒数 |
| `WBB_MESSAGE_MAX_CHARS` | `16000` | 单条文本最大字符数 |
| `WBB_MEDIA_MAX_BYTES` | `20971520` | 解码后媒体上限，绝对上限 25 MiB |
| `WBB_HOST_PORT` | `8000` | Compose 宿主机回环端口 |
| `UV_DEFAULT_INDEX` | 阿里云镜像 | uv/PyPI 索引 |

上表为 Docker Compose 值。Windows EXE 未显式设置路径变量时，`WBB_HOST` 默认
`127.0.0.1`，三个持久化路径默认位于 `%LOCALAPPDATA%\wechat-bot-bridge`；冻结程序只从
EXE 同目录读取 `.env`，不依赖启动时的当前工作目录。

服务级配置由 `/admin` 页面写入 SQLite `service_settings`：公共 API Token
自动生成；消息转发默认关闭，默认目标为
`http://127.0.0.1:8765/v1/sources/wechat-bridge/events`，单次超时 8 秒，
最大尝试 3 次。Token 和 HMAC Secret 都可在页面复制和轮换。

保持单进程、单 Uvicorn worker和单 iLink 长轮询消费者。横向扩容前必须先实现 poller 租约、共享队列和支持并发协调的存储。

应用日志使用统一格式，并在末尾附加白名单 JSON `context`，例如消息 ID、
类型、状态、错误码和重试次数。聊天正文、owner 标识、授权 token、
`context_token`、二维码内容和带查询参数的 URL 不会进入普通日志。
`WBB_LOG_LEVEL` 可设置为 `critical`、`error`、`warning`、`info`、`debug`
或 `trace`。

## 上游限制与排障

- 只有扫码账号能成为当前通道的唯一 `owner`，服务不提供联系人列表。
- 社区反馈主动发送通常受最近一次用户入站消息起约 24 小时的互动窗口约束，窗口内仍可能限频。
- `/ilink/bot/sendmessage failed (ret=-2)` 可能是上下文过期，也可能是限频，不能仅凭返回码认定唯一原因。
- 发生失败时停止密集重试，让 owner 重新向 Bot 发一条消息并降低频率。
- 当前内置客户端只对文字、图片、文件和出站输入状态完成了稳定建模，因此外部 API 和管理页面都不开放原生语音、视频或引用发送。
- iLink 只提供 Bot 向微信发送/取消输入状态的 `sendtyping`，`getupdates` 不包含微信用户的输入状态，因此微信端“正在输入”不能反向同步到网页。

协议来源：[腾讯 openclaw-weixin](https://github.com/Tencent/openclaw-weixin)、[Python 参考实现](https://github.com/corespeed-io/wechatbot/tree/main/python)。社区限制讨论：[互动窗口 #76](https://github.com/corespeed-io/wechatbot/issues/76)、[限频 #80](https://github.com/corespeed-io/wechatbot/issues/80)、[个人微信群 #53](https://github.com/corespeed-io/wechatbot/issues/53)。

## 常用运维命令

```bash
docker compose logs -f wechat-bot-bridge
docker compose build --pull
docker compose up -d
docker compose down
```

`docker compose down` 不会删除宿主机 `./data`。更新或迁移前应备份该目录，
并按密钥材料的级别保护备份。
