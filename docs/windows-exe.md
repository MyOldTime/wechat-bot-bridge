# Windows EXE 部署

## 目标与边界

Windows 发布包提供一个包含 Python 3.12.13、应用依赖和管理页面资源的
`wechat-bot-bridge.exe`。目标电脑不需要安装 Python、uv、Node.js 或 Docker。

双击 EXE 后，服务在 Windows 系统托盘运行，使用单进程、单 Uvicorn worker 和单 iLink 长轮询消费者。不要同时运行 EXE、Docker 和 uv 版本并指向
同一份数据库或微信授权文件。

软件图标是内含桥拱的对话气泡。矢量源文件位于 `src/wechat_bot_bridge/web/static/bridge.svg`。
`packaging/bridge.ico` 包含 16 到 256 像素的九档尺寸。构建时将 ICO 写入 EXE 资源，
资源管理器和托盘都从同一资源选择图标大小。

## 构建

构建机要求：

- Windows 10/11 x64；
- uv；
- 可安装项目锁定依赖的网络环境。

执行：

```powershell
.\scripts\build-windows.ps1
.\scripts\test-windows-release.ps1 -SkipBuild
```

构建脚本会：

1. 在 `build/uv-pyinstaller` 创建独立 uv 环境并精确使用 Python 3.12.13；
2. 执行 Ruff、Mypy 和全部 Pytest；
3. 通过仓库内 PyInstaller spec 生成单文件 EXE；
4. 复制 Windows 部署说明和许可证；
5. 生成 SHA-256 与 ZIP。

如果默认发布目录里正运行着旧版程序，可给两个脚本传入相同的 `-DistRoot`，在其他目录构建和烟测。
`build/` 与 `dist/` 都是生成目录，不加入 Git。PyInstaller 不是跨平台编译器：以后提供 Linux
独立程序时必须在 Linux 构建，并单独完成进程、路径和发布验收。

## 首次运行

1. 解压 ZIP 并双击 `wechat-bot-bridge.exe`。首次启动会创建当前用户的配置目录和
   `%LOCALAPPDATA%\wechat-bot-bridge\config\settings.json`，为管理员 `admin` 生成随机密码。
2. 找到通知区的桥形对话气泡图标，必要时展开任务栏隐藏图标。右键选“打开配置目录”，
   用记事本打开 `settings.json`，查看 `WBB_WEB_USERNAME` 和 `WBB_WEB_PASSWORD`。
3. 双击托盘图标，或右键选择“打开管理页面”，使用这组凭据登录。扫码授权后，用同一微信
   账号向 Bot 私聊一条消息。

从管理页的“MCP Token”复制 Token，在 MCP 客户端添加 Streamable HTTP 地址
`http://127.0.0.1:8000/mcp` 和 Bearer 请求头。工具名为 `send_wechat_message`。
[调用示例](external-api.md)

## 更改配置

EXE 的应用设置只从上述 JSON 文件读取。EXE 目录和当前工作目录中的 `.env` 以及进程的
`WBB_*` 环境变量均不参与加载。若系统未提供有效的 `LOCALAPPDATA` 绝对路径，程序从用户主目录的
`AppData\Local` 定位配置。文件使用 UTF-8 编码和大写的 `WBB_*` 应用配置键。默认监听
`127.0.0.1:8000`。修改设置后，从托盘退出，再次双击 EXE 即可生效。

| 配置项 | 用途 |
| --- | --- |
| `WBB_WEB_USERNAME` | 管理页用户名，默认为 `admin` |
| `WBB_WEB_PASSWORD` | 首次随机生成并保存在本机。可手动更换为至少 12 位的独立密码 |
| `WBB_HOST`、`WBB_PORT` | 监听地址和端口。端口填写 JSON 数字，例如 `8123` |

重启或升级会沿用已保存的密码。配置文件缺失时才生成新文件；已有文件若损坏、漏填密码或
含非法值，启动会提示修复并保留原文件。请将含明文密码的配置文件保存在个人账户可访问的目录中。

## 外部持久数据

默认目录：

```text
%LOCALAPPDATA%\wechat-bot-bridge\
├── config\settings.json
├── wechat-bridge.db
├── wechat-bot-bridge.lock
├── wechat-bot-bridge.log
└── wechat\credentials.json
```

- `wechat-bridge.db`：聊天、owner、游标、入站去重、发送审计和服务级配置；
- `credentials.json`：明文微信授权材料；
- `wechat-bot-bridge.lock`：单实例文件锁，不含业务数据。

升级或替换 EXE 不会覆盖该目录。`credentials.json` 与数据库应按密钥材料保护，不得加入发布
包、Git、聊天或普通日志。

如需自定义数据位置，在 `settings.json` 中增加 `"WBB_DATA_DIR": "D:/wechat-bot-bridge-data"`。
默认数据库和授权文件会跟随该数据目录。如需沿用已有的非标准文件名，可分别设置
`WBB_DATABASE_PATH` 和 `WBB_CREDENTIALS_PATH`。这三个路径均须使用绝对路径。
配置文件始终位于用户配置目录，移动数据前应先从托盘退出。

## 从旧版 EXE、uv 或 Docker 迁移

1. 停止旧 Bridge，确认端口 8000 和旧进程已经释放；
2. 备份旧 `data/`；
3. 旧 EXE 若使用默认数据目录，可直接复用原数据库和微信授权。其他部署可完整复制旧数据目录，
   或在 `settings.json` 中指向旧数据的绝对路径。旧版 `.env` 不会自动导入，需要沿用的端口、
   用户名、密码或路径请手动填入同名 JSON 字段。也可以在首次启动前创建配置文件，预先指定这些值；
4. 启动 EXE，访问 `/health/live` 和 `/admin`；
5. 用扫码 owner 向 Bot 发一条消息，再发送一条测试通知；
6. 验证完成前保留旧数据备份，但绝不能让两个进程同时使用它。

SQLite 的 `-wal`、`-shm` 文件只应在旧进程完全停止后处理。优先完整复制原数据目录，不要在
服务运行时只复制主数据库文件。

## 启停、升级与排障

- 正常停止时，右键托盘图标选择“退出”。服务会停止监听、关闭 SQLite 并释放单实例锁，图标随即消失。
- 排障可查看数据目录内的 `wechat-bot-bridge.log`。日志按大小轮换，每个文件最多 1 MiB，保留两份备份。
- 重复启动：第二个进程会显示已运行提示，确认后退出；
- 登录失败时，从托盘打开配置目录，核对用户名和密码。修改配置后需重启；
- 端口占用时，编辑 `config\settings.json` 中的 `WBB_PORT`，或停止占用同一端口的旧服务；
- 授权丢失：确认 `%LOCALAPPDATA%` 下的 `credentials.json` 未被移动或安全软件隔离；
- 升级：停止旧 EXE、备份数据目录、核对新 EXE 的 SHA-256，再原位替换程序并启动；
- 单文件首次启动稍慢：PyInstaller 需要把只读依赖释放到临时目录，这是正常行为。

发布烟测命令：

```powershell
.\scripts\test-windows-release.ps1 -SkipBuild
```

烟测在隔离的用户目录中启动真实 EXE，验证自动生成配置、忽略无效 `.env`、重启后用同一随机
密码登录，并在随机端口检查 MCP、托盘和正常退出。整个过程只使用临时数据库和测试配置。
