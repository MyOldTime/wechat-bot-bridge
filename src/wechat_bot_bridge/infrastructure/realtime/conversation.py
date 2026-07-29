"""管理页面 WebSocket 使用的进程内会话事件总线。"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass

from ...domain.models import ConversationMessage, MessageOrigin


@dataclass(frozen=True, slots=True)
class MessageBroadcast:
    message: ConversationMessage


@dataclass(frozen=True, slots=True)
class TypingBroadcast:
    active: bool
    origin: MessageOrigin


@dataclass(frozen=True, slots=True)
class ResetBroadcast:
    """通知管理页面丢弃账号退出前的全部进程内视图。"""


type ConversationBroadcast = MessageBroadcast | TypingBroadcast | ResetBroadcast


class ConversationHub:
    """向所有已连接管理页面广播会话变化，慢客户端只丢弃其最旧事件。"""

    def __init__(self, *, subscriber_queue_size: int = 100) -> None:
        self._subscriber_queue_size = subscriber_queue_size
        self._subscribers: set[asyncio.Queue[ConversationBroadcast]] = set()

    @property
    def subscriber_count(self) -> int:
        return len(self._subscribers)

    @asynccontextmanager
    async def subscribe(
        self,
    ) -> AsyncIterator[asyncio.Queue[ConversationBroadcast]]:
        queue: asyncio.Queue[ConversationBroadcast] = asyncio.Queue(
            maxsize=self._subscriber_queue_size
        )
        self._subscribers.add(queue)
        try:
            yield queue
        finally:
            self._subscribers.discard(queue)

    async def publish_message(self, message: ConversationMessage) -> None:
        self._publish(MessageBroadcast(message=message))

    async def publish_typing(
        self,
        *,
        active: bool,
        origin: MessageOrigin,
    ) -> None:
        self._publish(TypingBroadcast(active=active, origin=origin))

    async def publish_reset(self) -> None:
        """清空每个订阅者的旧事件，再把账号重置作为下一条唯一事实。"""

        for queue in tuple(self._subscribers):
            while True:
                try:
                    queue.get_nowait()
                except asyncio.QueueEmpty:
                    break
            try:
                queue.put_nowait(ResetBroadcast())
            except asyncio.QueueFull:
                # 队列刚被本方法清空；保留保护分支避免未来实现变更造成异常。
                pass

    def _publish(self, event: ConversationBroadcast) -> None:
        for queue in tuple(self._subscribers):
            if queue.full():
                try:
                    queue.get_nowait()
                except asyncio.QueueEmpty:
                    pass
            try:
                queue.put_nowait(event)
            except asyncio.QueueFull:
                # 极端竞争下宁可丢弃一个实时事件，页面仍可通过历史接口恢复。
                pass
