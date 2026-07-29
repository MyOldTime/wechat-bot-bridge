"""微信 iLink 内置客户端使用的错误类型。"""

from __future__ import annotations


class ILinkError(Exception):
    """所有 iLink 客户端错误的基类。"""


class ILinkApiError(ILinkError):
    """iLink HTTP 接口明确返回失败。"""

    def __init__(
        self,
        operation: str,
        *,
        http_status: int = 0,
        errcode: int = 0,
    ) -> None:
        super().__init__(f"{operation} failed")
        self.operation = operation
        self.http_status = http_status
        self.errcode = errcode

    @property
    def is_session_expired(self) -> bool:
        """腾讯 iLink 使用 -14 表示当前授权会话已经失效。"""

        return self.errcode == -14


class ILinkAuthError(ILinkError):
    """二维码登录或本地授权文件无效。"""


class ILinkProtocolError(ILinkError):
    """上游响应不符合当前支持的 iLink JSON 协议。"""


class ILinkNetworkError(ILinkError):
    """访问 iLink 接口时发生可重试的网络错误。"""


class ILinkMediaError(ILinkError):
    """媒体加密、上传或下载失败。"""

    def __init__(self, message: str, *, temporary: bool = False) -> None:
        super().__init__(message)
        self.temporary = temporary
