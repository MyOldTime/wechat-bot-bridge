"""Windows 通知区与 Uvicorn 共用一个进程，退出时等待服务完成 lifespan。"""

from __future__ import annotations

import logging
import os
import threading
import webbrowser

import uvicorn
import win32api
import win32con
import win32gui

from wechat_bot_bridge.core.config import Settings
from wechat_bot_bridge.core.paths import desktop_config_file

TITLE = "微信桥接 · wechat-bot-bridge"
_WINDOW_CLASS = "WeChatBotBridge.Tray"
_NOTIFY = win32con.WM_APP + 1
_STOPPED = win32con.WM_APP + 2
_OPEN, _QUIT, _CONFIG = 1, 2, 3


class WindowsTray:
    def __init__(self, server: uvicorn.Server, settings: Settings) -> None:
        self.server = server
        self.settings = settings
        self.failed = False
        self.taskbar_created = win32gui.RegisterWindowMessage("TaskbarCreated")

    def run(self) -> bool:
        module = win32api.GetModuleHandle(None)
        # PyInstaller 将 spec 的第一组多尺寸 ICO 写入 EXE 的资源 ID 1。
        self.icon = win32gui.LoadImage(
            module,
            1,
            win32con.IMAGE_ICON,
            win32api.GetSystemMetrics(win32con.SM_CXSMICON),
            win32api.GetSystemMetrics(win32con.SM_CYSMICON),
            win32con.LR_SHARED,
        )
        window_class = win32gui.WNDCLASS()
        window_class.hInstance = module
        window_class.lpszClassName = _WINDOW_CLASS
        window_class.lpfnWndProc = self._window_proc
        atom = win32gui.RegisterClass(window_class)
        self.window = win32gui.CreateWindow(atom, TITLE, 0, 0, 0, 0, 0, 0, 0, module, None)
        worker = threading.Thread(target=self._serve, name="bridge-server")
        try:
            self._show_icon()
            worker.start()
            win32gui.PumpMessages()
        finally:
            self.server.should_exit = True
            if worker.ident is not None:
                worker.join()
            try:
                win32gui.Shell_NotifyIcon(win32gui.NIM_DELETE, (self.window, 1))
            finally:
                win32gui.DestroyWindow(self.window)
                win32gui.UnregisterClass(atom, module)
        return not self.failed

    def _serve(self) -> None:
        try:
            self.server.run()
        except (Exception, SystemExit):
            self.failed = True
            logging.getLogger("wechat_bot_bridge.tray").error("桥接服务启动或运行失败")
        finally:
            win32gui.PostMessage(self.window, _STOPPED, 0, 0)

    def _show_icon(self) -> None:
        flags = win32gui.NIF_ICON | win32gui.NIF_MESSAGE | win32gui.NIF_TIP
        win32gui.Shell_NotifyIcon(
            win32gui.NIM_ADD, (self.window, 1, flags, _NOTIFY, self.icon, TITLE)
        )

    def _open_admin(self) -> None:
        if not self.server.started or self.server.should_exit:
            return
        host = {"0.0.0.0": "127.0.0.1", "::": "::1"}.get(self.settings.host, self.settings.host)
        if ":" in host:
            host = f"[{host}]"
        webbrowser.open(f"http://{host}:{self.settings.port}/admin")

    def _menu(self) -> None:
        menu = win32gui.CreatePopupMenu()
        try:
            flags = win32con.MF_STRING
            if not self.server.started or self.server.should_exit:
                flags |= win32con.MF_GRAYED
            win32gui.AppendMenu(menu, flags, _OPEN, "打开管理页面")
            win32gui.AppendMenu(menu, win32con.MF_STRING, _CONFIG, "打开配置目录")
            win32gui.AppendMenu(menu, win32con.MF_STRING, _QUIT, "退出")
            x, y = win32gui.GetCursorPos()
            win32gui.SetForegroundWindow(self.window)
            win32gui.TrackPopupMenu(menu, win32con.TPM_RIGHTBUTTON, x, y, 0, self.window, None)
            win32gui.PostMessage(self.window, win32con.WM_NULL, 0, 0)
        finally:
            win32gui.DestroyMenu(menu)

    def _window_proc(self, hwnd: int, msg: int, wparam: int, lparam: int) -> int:
        if msg == self.taskbar_created:
            self._show_icon()
        elif msg == _NOTIFY and lparam == win32con.WM_LBUTTONDBLCLK:
            self._open_admin()
        elif msg == _NOTIFY and lparam == win32con.WM_RBUTTONUP:
            self._menu()
        elif msg == win32con.WM_COMMAND and win32api.LOWORD(wparam) == _OPEN:
            self._open_admin()
        elif msg == win32con.WM_COMMAND and win32api.LOWORD(wparam) == _CONFIG:
            directory = desktop_config_file().parent
            try:
                os.startfile(directory)
            except OSError:
                win32gui.MessageBox(self.window, str(directory), TITLE, win32con.MB_ICONERROR)
        elif msg == win32con.WM_CLOSE or (
            msg == win32con.WM_COMMAND and win32api.LOWORD(wparam) == _QUIT
        ):
            self.server.should_exit = True
        elif msg == _STOPPED:
            win32gui.PostQuitMessage(0)
        else:
            return win32gui.DefWindowProc(hwnd, msg, wparam, lparam)
        return 0
