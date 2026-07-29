"""管理页面输入状态的服务端租约。"""

from __future__ import annotations

import asyncio
import logging

from ..domain.models import (
    MessageOrigin,
    OutboundMessage,
    TypingCancelPayload,
    TypingStartPayload,
)
from .messaging import NotificationService

logger = logging.getLogger(__name__)


class TypingLeaseService:
    """发送输入状态，并确保页面失联后仍会在租约到期时取消。"""

    def __init__(
        self,
        *,
        notifications: NotificationService,
        timeout_seconds: float,
    ) -> None:
        self._notifications = notifications
        self._timeout_seconds = timeout_seconds
        self._lock = asyncio.Lock()
        self._active = False
        self._last_start_message: OutboundMessage | None = None
        self._expiry_task: asyncio.Task[None] | None = None

    @property
    def active(self) -> bool:
        return self._active

    async def start(self) -> OutboundMessage:
        async with self._lock:
            if self._active and self._last_start_message is not None:
                # 页面持续键入只需刷新服务端租约，不重复调用 iLink。
                self._renew_expiry()
                return self._last_start_message
            message = await self._notifications.send(
                payload=TypingStartPayload(),
                origin=MessageOrigin.WEB_ADMIN,
            )
            self._active = True
            self._last_start_message = message
            self._renew_expiry()
            return message

    async def stop(self) -> OutboundMessage:
        """显式取消输入状态；即使本地状态已过期也会向微信发送一次。"""

        async with self._lock:
            self._cancel_expiry_task()
            try:
                return await self._notifications.send(
                    payload=TypingCancelPayload(),
                    origin=MessageOrigin.WEB_ADMIN,
                )
            finally:
                self._active = False
                self._last_start_message = None

    async def cancel_if_active(self) -> None:
        """在发送正文或关闭服务前尽力取消，不让取消失败阻断主流程。"""

        async with self._lock:
            if not self._active:
                return
            self._cancel_expiry_task()
            try:
                await self._notifications.send(
                    payload=TypingCancelPayload(),
                    origin=MessageOrigin.WEB_ADMIN,
                )
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.warning(
                    "取消管理页面输入状态失败",
                    exc_info=True,
                )
            finally:
                self._active = False
                self._last_start_message = None

    async def close(self) -> None:
        await self.cancel_if_active()
        self._cancel_expiry_task()

    async def _expire(self) -> None:
        try:
            await asyncio.sleep(self._timeout_seconds)
            await self.cancel_if_active()
        except asyncio.CancelledError:
            return

    def _renew_expiry(self) -> None:
        self._cancel_expiry_task()
        self._expiry_task = asyncio.create_task(
            self._expire(),
            name="web-typing-lease",
        )

    def _cancel_expiry_task(self) -> None:
        task = self._expiry_task
        self._expiry_task = None
        if task is not None and task is not asyncio.current_task():
            task.cancel()
