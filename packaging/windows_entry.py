"""Windows GUI 子系统入口。无参数或 serve 均启动托盘应用。"""

from __future__ import annotations

import sys
from importlib.metadata import version

import uvicorn
import win32con
import win32gui
from pydantic import ValidationError
from windows_tray import TITLE, WindowsTray

from wechat_bot_bridge.bootstrap.app import create_app
from wechat_bot_bridge.bootstrap.single_instance import InstanceAlreadyRunningError, single_instance
from wechat_bot_bridge.core.desktop_config import DesktopConfigError, DesktopSettings
from wechat_bot_bridge.core.logging import configure_logging
from wechat_bot_bridge.core.paths import desktop_config_file


def main() -> int:
    args = sys.argv[1:]
    if args and args != ["serve"]:
        info = (
            version("wechat-bot-bridge")
            if args == ["--version"]
            else "双击程序启动，在系统托盘打开管理页面或退出。"
        )
        if sys.stdout is None:
            win32gui.MessageBox(0, info, TITLE, win32con.MB_ICONINFORMATION)
        else:
            print(info)
        return 0 if args in (["--version"], ["--help"], ["-h"]) else 2

    try:
        settings = DesktopSettings()
        with single_instance(settings.data_dir):
            log_path = settings.data_dir / "wechat-bot-bridge.log"
            configure_logging(settings.log_level, log_file=log_path)
            server = uvicorn.Server(
                uvicorn.Config(
                    create_app(settings),
                    host=settings.host,
                    port=settings.port,
                    log_level=settings.log_level,
                    log_config=None,
                    workers=1,
                    access_log=False,
                    timeout_graceful_shutdown=60,
                )
            )
            if WindowsTray(server, settings).run():
                return 0
            error = f"服务启动或运行失败。请检查端口和日志。\n{desktop_config_file()}\n{log_path}"
    except InstanceAlreadyRunningError:
        win32gui.MessageBox(
            0, "程序已在使用这个数据目录，请查看系统托盘。", TITLE, win32con.MB_ICONINFORMATION
        )
        return 2
    except DesktopConfigError as exc:
        error = str(exc)
    except ValidationError:
        error = f"配置项无效。请检查字段名、值及至少 12 位的管理密码。\n{desktop_config_file()}"
    except Exception:
        error = "无法启动桥接服务。请检查数据目录的写入权限及 Windows 通知区域。"
    win32gui.MessageBox(0, error, TITLE, win32con.MB_ICONERROR)
    return 1


if __name__ == "__main__":
    sys.exit(main())
