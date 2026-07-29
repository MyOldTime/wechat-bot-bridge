"""微信网关依赖的最小客户端契约。"""

from __future__ import annotations

from collections.abc import Callable
from typing import Protocol

from .ilink.models import Credentials, DownloadedMedia, IncomingMessage, UpdateBatch
from .ilink.transport import ILinkTransport


class GatewayClient(Protocol):
    """网关使用的 iLink 高层能力；测试可注入无网络实现。"""

    @property
    def credentials(self) -> Credentials: ...

    async def get_updates(self, cursor: str) -> UpdateBatch: ...

    async def send_text(
        self,
        *,
        user_id: str,
        context_token: str,
        text: str,
    ) -> None: ...

    async def send_image(
        self,
        *,
        user_id: str,
        context_token: str,
        data: bytes,
        caption: str | None,
    ) -> None: ...

    async def send_file(
        self,
        *,
        user_id: str,
        context_token: str,
        data: bytes,
        file_name: str,
        caption: str | None,
    ) -> None: ...

    async def set_typing(
        self,
        *,
        user_id: str,
        context_token: str,
        active: bool,
    ) -> None: ...

    async def download(self, message: IncomingMessage) -> DownloadedMedia | None: ...

    async def notify_start(self) -> None: ...

    async def notify_stop(self) -> None: ...


type TransportFactory = Callable[[], ILinkTransport]
