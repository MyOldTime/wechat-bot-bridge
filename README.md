<img src="src/wechat_bot_bridge/web/static/bridge.svg" width="72" height="72" alt="微信桥接图标">

# wechat-bot-bridge

把构建结果、任务进度或告警发到自己的微信。Bridge 通过微信 iLink Bot 接收和发送消息，对外提供一个 MCP 工具 `send_wechat_message`。可直接运行 Windows 托盘版，也可用 Docker Compose 部署。

**[下载 Windows x64](https://github.com/MyOldTime/wechat-bot-bridge/releases/latest/download/wechat-bot-bridge-windows-x64.zip)** · [版本与校验值](https://github.com/MyOldTime/wechat-bot-bridge/releases) · [MCP 接入指南](docs/external-api.md)

## Windows 快速开始

1. 下载并解压 ZIP，双击 `wechat-bot-bridge.exe`。Windows 10/11 x64 可直接运行，无需安装 Python。桥形对话气泡图标会出现在系统托盘，必要时展开任务栏的隐藏图标。
2. 右键图标，选 **打开配置目录**。首次启动会创建 `settings.json`，为默认用户 `admin` 生成随机密码。在文件中查看 `WBB_WEB_PASSWORD`。
3. 双击托盘图标打开管理页面，使用这组凭据登录并扫码。**用扫码的微信账号向 Bot 私聊一条消息**，Bridge 才能取得主动通知所需的会话上下文。
4. 在管理页的 **MCP Token** 中复制令牌，按下一节连接客户端。右键托盘选 **退出** 可停止服务，重启后继续使用原密码和聊天记录。

默认配置文件位于 `%LOCALAPPDATA%\wechat-bot-bridge\config\settings.json`。调整端口或密码后重新启动 EXE。升级时从托盘退出，再替换 EXE。安装、数据迁移和排障见 [Windows 部署指南](docs/windows-exe.md)。

## 通过 MCP 发送第一条通知

| 客户端设置 | 值 |
| --- | --- |
| 传输 | Streamable HTTP |
| 服务地址 | `http://127.0.0.1:8000/mcp` |
| 请求头 | `Authorization: Bearer <从管理页复制的 MCP Token>` |
| 工具 | `send_wechat_message` |

连接后，客户端通过 `tools/list` 可以查看每个字段的含义、合法取值和当前部署上限。下面是一组完整的工具参数。多行通知建议用 Markdown 的短标题和列表，Bridge 按微信文本发送并保留换行与标记。

```json
{
  "message": {
    "type": "text",
    "text": "## 构建完成\n- 版本：v0.1.0\n- 测试：通过"
  }
}
```

调用成功后，`structuredContent.status` 为 `sent`，`id` 是本地审计 ID，`msg_id` 是微信返回的主消息 ID（可能为空）。每次调用独立发送，超时后请先核实微信或管理页历史，再决定是否重试。

| 动作 | 用途 |
| --- | --- |
| `text` | 发送文字通知 |
| `image`、`file` | 发送以标准 Base64 编码的图片或普通文件，可附说明 |
| `typing_start`、`typing_cancel` | 显示或取消“正在输入”，不产生聊天气泡 |

管理页使用独立的 Basic Auth，可查看 SQLite 聊天历史、发送消息和轮换 MCP Token。还可以配置单向 HTTP Webhook，将**微信入站**事件签名后转发给下游。该功能默认关闭。[管理接口与转发契约](docs/api.md)

## 支持范围

微信 iLink Bot 是扫码账号与 Bot 的一对一私聊通道。Bridge 只向这一个已绑定账号发送通知，无法读取其他好友会话或普通微信群。长时间没有互动或遇到限频时，让扫码账号再给 Bot 发一条消息；上游的 `ret=-2` 不能单独判定具体原因。

运行一个 Bridge 进程和一个 Uvicorn worker。微信只应有一个长轮询消费者，同一数据目录也只允许一个活动实例。管理页的 **退出登录** 会清除本机微信授权和聊天记录。只想停止服务时，请使用托盘的 **退出** 或 `docker compose down`。

## Docker Compose 部署

在仓库根目录准备配置并设置至少 12 位的独立管理密码。Linux 宿主机还需让容器用户（UID/GID `10001`）可写数据目录。

```bash
cp .env.example .env
# 编辑 .env，替换 WBB_WEB_PASSWORD 的示例值
mkdir -p data/wechat
sudo chown -R 10001:10001 ./data
chmod 700 ./data ./data/wechat
docker compose up -d --build
curl http://127.0.0.1:8000/health/live
```

打开 [本地管理页面](http://127.0.0.1:8000/admin) 完成扫码和首次私聊。Compose 默认只发布到宿主机回环地址，SQLite 和微信授权由 `./data:/data` 持久化。跨机器连接请使用 HTTPS 反向代理或受控私网，并设置实际访问源 `WBB_MCP_PUBLIC_ORIGIN`。

## 配置

Windows EXE 只从用户配置目录的 JSON 读取应用设置。源码开发读取工作目录的 `.env`，Compose 从宿主机注入环境变量。MCP Token 与转发密钥由管理页维护。

| 设置 | 默认值及用途 |
| --- | --- |
| `WBB_WEB_USERNAME`、`WBB_WEB_PASSWORD` | 管理员默认 `admin`。EXE 首次生成密码；源码或 Docker 部署须自行设置密码以启用管理页 |
| `WBB_HOST`、`WBB_PORT` | EXE 默认 `127.0.0.1:8000`，容器内部监听 `0.0.0.0:8000` |
| `WBB_MCP_PUBLIC_ORIGIN` | 可选的受信访问源，如 `https://bridge.example.test` |
| `WBB_DATA_DIR`、`WBB_DATABASE_PATH`、`WBB_CREDENTIALS_PATH` | 默认数据根目录分别为 Windows 的 `%LOCALAPPDATA%\wechat-bot-bridge` 和容器的 `/data`。默认文件为 `wechat-bridge.db` 和 `wechat/credentials.json` |
| `WBB_MESSAGE_MAX_CHARS`、`WBB_MEDIA_MAX_BYTES` | 默认 16000 字符和 20 MiB；媒体硬上限 25 MiB |

队列、媒体超时、日志、页面令牌和输入状态租约等设置见 [配置示例](.env.example)。`settings.json` 含管理密码，`credentials.json` 含明文微信授权，数据库含聊天正文和媒体，请将这些文件保存在私有目录中，并保护备份。

## 开发与贡献

项目固定使用 Python 3.12.13 和 uv。开发时在仓库根目录新建 `.env` 并设置管理密码，持久数据默认写入本地 `data/`。

```dotenv
WBB_WEB_USERNAME=admin
WBB_WEB_PASSWORD=<替换为至少 12 位的独立密码>
```

```bash
uv python install 3.12.13
uv sync --frozen
uv run wechat-bot-bridge serve
```

[贡献指南和测试命令](CONTRIBUTING.md) · [架构设计](docs/architecture.md) · [iLink 协议实现与来源](docs/ilink-protocol.md) · [MIT 许可证](LICENSE)
