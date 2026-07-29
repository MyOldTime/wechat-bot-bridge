"""微信 iLink 协议常量。"""

from __future__ import annotations

from importlib.metadata import PackageNotFoundError, version

DEFAULT_API_BASE_URL = "https://ilinkai.weixin.qq.com"
FIXED_QR_BASE_URL = DEFAULT_API_BASE_URL
DEFAULT_CDN_BASE_URL = "https://novac2c.cdn.weixin.qq.com/c2c"
ILINK_APP_ID = "bot"

try:
    CHANNEL_VERSION = version("wechat-bot-bridge")
except PackageNotFoundError:
    CHANNEL_VERSION = "0.1.0"

BOT_AGENT = f"wechat-bot-bridge/{CHANNEL_VERSION}"


def client_version_number(value: str = CHANNEL_VERSION) -> str:
    """按 0x00MMNNPP 规则生成 `iLink-App-ClientVersion`。"""

    parts = value.split(".")
    parsed: list[int] = []
    for part in parts[:3]:
        numeric = part.split("-", 1)[0].split("+", 1)[0]
        try:
            parsed.append(int(numeric) & 0xFF)
        except ValueError:
            parsed.append(0)
    parsed.extend([0] * (3 - len(parsed)))
    return str((parsed[0] << 16) | (parsed[1] << 8) | parsed[2])


ILINK_APP_CLIENT_VERSION = client_version_number()
