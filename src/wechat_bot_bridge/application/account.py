"""管理端驱动的微信账号登录与清理用例。"""

from __future__ import annotations

import asyncio
import logging

from ..domain.ports import BridgeStore, ConversationPublisher, WeChatGateway

logger = logging.getLogger(__name__)


class AccountLifecycleService:
    """串行编排登录控制、退出停机和全部持久状态清理。"""

    def __init__(
        self,
        *,
        store: BridgeStore,
        gateway: WeChatGateway,
        publisher: ConversationPublisher,
        operation_lock: asyncio.Lock,
    ) -> None:
        self._store = store
        self._gateway = gateway
        self._publisher = publisher
        self._operation_lock = operation_lock

    async def start_login(self) -> None:
        """请求后台网关生成二维码；重复请求保持幂等。"""

        async with self._operation_lock:
            await self._gateway.start_login()

    async def submit_verification_code(self, code: str) -> None:
        """把管理页面输入的微信配对数字交给当前登录流程。"""

        async with self._operation_lock:
            await self._gateway.submit_verification_code(code)

    async def logout_and_restart_login(self) -> None:
        """停止旧会话、清空全部存储，再启动一轮新的扫码登录。"""

        async with self._operation_lock:
            logger.info("开始重置微信账号及本地业务数据")
            # 网关退出会先停止生产新事件，并等待入站分发队列排空。
            await self._gateway.logout()
            await self._store.clear_all()
            await self._publisher.publish_reset()
            await self._gateway.start_login()
            logger.info("微信账号及本地业务数据已重置")
