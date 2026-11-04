# 贡献指南

## 本地环境

1. 安装 uv，克隆仓库并进入根目录。
2. 运行 `uv python install 3.12.13` 和 `uv sync --frozen`。
3. 修改前阅读 [仓库维护约定](AGENT.md) 和与改动相关的 [架构文档](docs/architecture.md)。

源码版从当前目录读取 `.env`。调试管理页面时，设置独立的 `WBB_WEB_PASSWORD` 并运行
`uv run wechat-bot-bridge serve`。自动化测试使用临时 SQLite 和模拟微信网关，无需登录真实微信账号。

## 验证改动

```bash
uv run --no-sync ruff check .
uv run --no-sync ruff format --check .
uv run --no-sync mypy src
uv run --no-sync pytest
```

新增依赖时通过 `uv add` 更新 `pyproject.toml` 和 `uv.lock`。改动 MCP 参数或回执时，用
`tools/list` 和 `tools/call` 验证调用方看到的契约，并同步 [MCP 接入指南](docs/external-api.md)。
Windows 打包改动应在 Windows x64 机器上构建 EXE，并完成托盘和启动烟测。[构建和烟测命令](docs/windows-exe.md)

## 提交 Issue 或 Pull Request

报告问题时请注明 Bridge 版本、操作系统、启动方式（Windows、Docker 或源码）、复现步骤和错误码。
分享示例时请使用虚构的消息与令牌，避免上传 `settings.json`、`.env`、微信授权文件、数据库或私人聊天内容。

Pull Request 请说明改动对使用者的影响、运行过的验证，以及需要同步的配置或文档。
