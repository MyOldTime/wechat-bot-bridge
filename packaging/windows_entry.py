"""PyInstaller Windows 入口；双击无参数启动时默认进入 serve。"""

from __future__ import annotations

import sys

from wechat_bot_bridge.bootstrap.cli import main

if __name__ == "__main__":
    arguments = sys.argv[1:] or ["serve"]
    main(arguments)
