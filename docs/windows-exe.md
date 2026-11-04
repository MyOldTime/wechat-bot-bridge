# Windows EXE 部署

## 目标与边界

Windows 发布包提供一个包含 Python 3.12.13、应用依赖和管理页面资源的
`wechat-bot-bridge.exe`。目标电脑不需要安装 Python、uv、Node.js 或 Docker。

EXE 仍是单进程、单 Uvicorn worker、单 iLink 长轮询消费者。它不是 Windows Service，默认
保留控制台用于安全日志和 `Ctrl+C` 优雅退出。不要同时运行 EXE、Docker 和 uv 版本并指向
同一份数据库或微信授权文件。

## 构建

构建机要求：

- Windows 10/11 x64；
- uv；
- 可安装项目锁定依赖的网络环境。

执行：

```powershell
cd D:\development\code\ai\wechat-bot-bridge
.\scripts\build-windows.ps1
.\scripts\test-windows-release.ps1 -SkipBuild
```

构建脚本会：

1. 在 `build/uv-pyinstaller` 创建独立 uv 环境并精确使用 Python 3.12.13；
2. 执行 Ruff、Mypy 和全部 Pytest；
3. 通过仓库内 PyInstaller spec 生成单文件 EXE；
4. 复制 Windows 配置示例、部署说明和许可证；
5. 生成 SHA-256 与 ZIP。

`build/` 与 `dist/` 都是生成目录，不加入 Git。PyInstaller 不是跨平台编译器：以后提供 Linux
独立程序时必须在 Linux 构建，并单独完成进程、路径和发布验收。

## 首次运行

解压 `dist/wechat-bot-bridge-windows-x64.zip`，然后：

```powershell
Copy-Item windows.env.example .env
notepad .env
```

必须把示例密码替换为至少 12 位的独立密码：

```dotenv
WBB_WEB_USERNAME=admin
WBB_WEB_PASSWORD=<独立强密码>
WBB_HOST=127.0.0.1
WBB_PORT=8000
```

启动：

```powershell
.\wechat-bot-bridge.exe serve
```

也可以直接双击；冻结入口在没有参数时自动使用唯一的 `serve` 子命令。打开
`http://127.0.0.1:8000/admin`，输入 `.env` 中的 Basic 用户名和密码，再完成微信扫码。

没有 `.env` 或没有 `WBB_WEB_PASSWORD` 时 Bridge 仍可启动，但管理页面按安全策略保持关闭，
因此无法完成扫码或管理 Token。

## 外部持久数据

默认目录：

```text
%LOCALAPPDATA%\wechat-bot-bridge\
├── wechat-bridge.db
├── wechat-bot-bridge.lock
└── wechat\credentials.json
```

- `wechat-bridge.db`：聊天、owner、游标、入站去重、发送审计和服务级配置；
- `credentials.json`：明文微信授权材料；
- `wechat-bot-bridge.lock`：单实例文件锁，不含业务数据。

升级或替换 EXE 不会覆盖该目录。`credentials.json` 与数据库应按密钥材料保护，不得加入发布
包、Git、聊天或普通日志。

如需改变位置，应在 EXE 同目录 `.env` 中同时使用绝对路径：

```dotenv
WBB_DATA_DIR=D:\wechat-bot-bridge-data
WBB_DATABASE_PATH=D:\wechat-bot-bridge-data\wechat-bridge.db
WBB_CREDENTIALS_PATH=D:\wechat-bot-bridge-data\wechat\credentials.json
```

不要只修改其中一个路径；同一部署的锁、数据库和授权应保持在同一个受保护目录中。

## 从 uv 或 Docker 迁移

1. 停止旧 Bridge，确认端口 8000 和旧进程已经释放；
2. 备份旧 `data/`；
3. 将 `wechat-bridge.db` 和 `wechat/credentials.json` 复制到 EXE 默认目录，或者在 `.env`
   中明确指向旧数据的绝对路径；
4. 启动 EXE，访问 `/health/live` 和 `/admin`；
5. 用扫码 owner 向 Bot 发一条消息，再发送一条测试通知；
6. 验证完成前保留旧数据备份，但绝不能让两个进程同时使用它。

SQLite 的 `-wal`、`-shm` 文件只应在旧进程完全停止后处理。优先完整复制原数据目录，不要在
服务运行时只复制主数据库文件。

## 启停、升级与排障

- 正常停止：控制台按 `Ctrl+C`，等待“已安全停止”日志；
- 重复启动：第二个进程会报告当前数据目录已被使用并立即退出；
- 管理页面 503：检查 EXE 同目录 `.env` 是否存在，以及 `WBB_WEB_PASSWORD` 是否有效；
- 端口占用：修改 `WBB_PORT`，或停止占用同一端口的旧服务；
- 授权丢失：确认 `%LOCALAPPDATA%` 下的 `credentials.json` 未被移动或安全软件隔离；
- 升级：停止旧 EXE、备份数据目录、核对新 EXE 的 SHA-256，再原位替换程序并启动；
- 单文件首次启动稍慢：PyInstaller 需要把只读依赖释放到临时目录，这是正常行为。

发布烟测命令：

```powershell
.\scripts\test-windows-release.ps1 -SkipBuild
```

烟测使用随机端口、临时 SQLite 和假管理密码，不读取真实 `data/`、`.env` 或微信授权；结束后
会停止它启动的整个 one-file 进程树并清理临时目录。
