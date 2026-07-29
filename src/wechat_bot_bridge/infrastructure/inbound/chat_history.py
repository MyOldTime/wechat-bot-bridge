"""把统一入站事件写入聊天记录并通知管理页面。"""

from __future__ import annotations

from ...domain.models import InboundEvent
from ...domain.ports import BridgeStore, ConversationPublisher


class ChatHistorySink:
    """复用 InboundSink 扩展点保存微信入站聊天。"""

    def __init__(
        self,
        *,
        store: BridgeStore,
        publisher: ConversationPublisher,
    ) -> None:
        self._store = store
        self._publisher = publisher

    async def deliver(self, event: InboundEvent) -> None:
        message = await self._store.record_inbound_conversation(event)
        await self._publisher.publish_message(message)

    async def close(self) -> None:
        return None
