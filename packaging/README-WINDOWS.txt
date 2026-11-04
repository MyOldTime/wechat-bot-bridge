wechat-bot-bridge Windows x64
==============================

1. 解压发布包，双击 wechat-bot-bridge.exe。首次启动会创建
   %LOCALAPPDATA%\wechat-bot-bridge\config\settings.json，并为 admin 生成随机管理密码。
   后续启动和升级会沿用已保存的配置。
2. 程序在 Windows 通知区域显示桥形对话气泡图标，
   如果没有看到，展开任务栏的隐藏图标。
3. 右键托盘选“打开配置目录”，用记事本打开 settings.json，找到 WBB_WEB_USERNAME 和
   WBB_WEB_PASSWORD。双击托盘图标打开管理页面，输入这组凭据，扫码授权，
   再用同一微信账号向 Bot 私聊一条消息。
4. 从管理页的“MCP Token”复制令牌。将 MCP 客户端的传输设为 Streamable HTTP，
   地址为 http://127.0.0.1:8000/mcp，请求头为 Authorization: Bearer <你的 Token>。
   如修改了 WBB_PORT，请同步修改客户端地址。
5. 在 MCP 客户端查看 send_wechat_message 的字段说明和当前发送上限。
   多行正文建议用 Markdown 编排，标记按微信文本发送。例如
   {"message":{"type":"text","text":"## 构建完成\n- 版本：v1.2.0\n- 测试：通过"}}
   每次调用都会独立发送。超时后重复调用可能产生重复通知。
6. 右键托盘图标，选择“退出”停止服务。退出程序会保留账号和历史，可再次双击启动。

更改密码、端口或其他 WBB_* 配置时，请编辑 settings.json 并从托盘退出后重启。
旧版 EXE 的 .env 不会导入。曾自定义端口或数据路径时，请将相应值填入 settings.json。
默认数据目录为 %LOCALAPPDATA%\wechat-bot-bridge，内含 SQLite、锁文件、安全运行日志和
wechat\credentials.json。登录配置含明文密码，请与凭据和数据库一起保护。升级时先从托盘
退出，待进程结束后再替换 EXE。同一个数据目录只能运行一个实例。
