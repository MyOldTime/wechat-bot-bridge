wechat-bot-bridge Windows x64
================================

1. 将 windows.env.example 复制为 .env。
2. 把 WBB_WEB_PASSWORD 改为至少 12 位且与示例不同的密码。
3. 双击 wechat-bot-bridge.exe，或在终端运行：

   .\wechat-bot-bridge.exe serve

4. 打开 http://127.0.0.1:8000/admin，使用 .env 中的 Basic 用户名和密码登录。

默认持久化目录：

  %LOCALAPPDATA%\wechat-bot-bridge\
  ├── wechat-bridge.db
  ├── wechat-bot-bridge.lock
  └── wechat\credentials.json

credentials.json 含明文微信授权材料。请限制目录权限、保护备份，不要把它复制到
聊天、日志、工单或 Git。升级 EXE 不会覆盖该目录。

同一个持久化目录只允许一个 Bridge 进程运行。退出时请在控制台按 Ctrl+C，等待服务完成
SQLite、入站队列和微信长轮询清理后再关闭窗口。
