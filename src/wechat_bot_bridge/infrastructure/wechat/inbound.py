"""微信入站消息的 owner 校验、规范化、去重与媒体处理。"""

from __future__ import annotations

import asyncio
import hashlib
import logging
from typing import Final, Protocol

from ...domain.models import (
    ConversationMessageType,
    InboundEvent,
    InboundMedia,
    InboundMessageBody,
    InboundQuote,
    InboundSender,
)
from ...domain.ports import InboundPublisher, WeChatStateStore
from ...domain.references import scoped_protocol_item_id
from .ilink.errors import ILinkMediaError
from .ilink.media import downloaded_media_mime, downloaded_media_name
from .ilink.models import Credentials, DownloadedMedia, IncomingMessage

logger = logging.getLogger(__name__)

_MEDIA_PLACEHOLDERS: Final[dict[str, frozenset[str]]] = {
    "image": frozenset({"[image]", "[图片]"}),
    "voice": frozenset({"[voice]", "[语音]"}),
    "file": frozenset({"[file]", "[文件]"}),
    "video": frozenset({"[video]", "[视频]"}),
    "text": frozenset(),
}


class InboundMediaClient(Protocol):
    async def download(self, message: IncomingMessage) -> DownloadedMedia | None: ...


class InboundMessageProcessor:
    """处理单条可信 iLink 入站消息，不持有网关生命周期状态。"""

    def __init__(
        self,
        *,
        store: WeChatStateStore,
        dispatcher: InboundPublisher,
        media_download_timeout_seconds: float,
    ) -> None:
        self._store = store
        self._dispatcher = dispatcher
        self._media_download_timeout_seconds = media_download_timeout_seconds

    async def process(
        self,
        message: IncomingMessage,
        *,
        client: InboundMediaClient,
        credentials: Credentials,
        session_fingerprint: str,
    ) -> None:
        """校验唯一 owner 后发布一条规范化、持久去重的事件。"""

        if message.user_id != credentials.user_id:
            logger.warning(
                "忽略非当前 owner 身份发送的微信入站消息",
                extra={"message_type": message.type},
            )
            return
        if not message.context_token:
            logger.warning(
                "忽略缺少会话上下文的微信入站消息",
                extra={"message_type": message.type},
            )
            return
        await self._store.save_owner_context(
            user_id=message.user_id,
            context_token=message.context_token,
            credential_fingerprint=session_fingerprint,
        )
        event = normalize_inbound_event(message, account_id=credentials.account_id)
        if not await self._store.remember_inbound_event(event):
            return

        media = await self._download_media(client, message)
        if media is not None:
            event = event.model_copy(update={"media": media})
        await self._dispatcher.publish(event)

    async def _download_media(
        self,
        client: InboundMediaClient,
        message: IncomingMessage,
    ) -> InboundMedia | None:
        if message.type == "text" or not message.media:
            return None
        try:
            async with asyncio.timeout(self._media_download_timeout_seconds):
                downloaded = await client.download(message)
        except asyncio.CancelledError:
            raise
        except (ILinkMediaError, TimeoutError):
            logger.warning(
                "下载微信入站媒体失败，保留消息元数据继续分发",
                extra={"message_type": message.type},
            )
            return None
        except Exception:
            logger.exception(
                "下载微信入站媒体时发生未预期异常，保留消息元数据继续分发",
                extra={"message_type": message.type},
            )
            return None
        if downloaded is None:
            return None
        return InboundMedia(
            data=downloaded.data,
            mime_type=downloaded_media_mime(downloaded),
            file_name=downloaded_media_name(downloaded),
        )


def normalize_inbound_event(
    message: IncomingMessage,
    *,
    account_id: str,
) -> InboundEvent:
    """将协议消息转换为可持久化、可转发的版本化领域事件。"""

    event_id = hashlib.sha256(f"{account_id}\0{message.upstream_id}".encode()).hexdigest()
    quote = None
    if message.quoted_message is not None:
        quote = InboundQuote(
            title=message.quoted_message.title,
            type=ConversationMessageType(message.quoted_message.type),
            text=message.quoted_message.text,
            protocol_item_id=scoped_protocol_item_id(
                account_id,
                message.quoted_message.item_id,
            ),
        )
    return InboundEvent(
        event_id=event_id,
        occurred_at=message.timestamp,
        sender=InboundSender(user_id=message.user_id),
        message=InboundMessageBody(
            type=message.type,
            text=normalized_inbound_text(message),
        ),
        quote=quote,
        protocol_item_id=scoped_protocol_item_id(account_id, message.item_id),
        protocol_message_id=scoped_protocol_item_id(account_id, message.message_id),
    )


def normalized_inbound_text(message: IncomingMessage) -> str:
    """过滤上游媒体占位符，只保留真实说明或语音转写。"""

    placeholders = {value.casefold() for value in _MEDIA_PLACEHOLDERS[message.type]}
    meaningful_lines: list[str] = []
    for line in message.text.splitlines():
        normalized = line.strip()
        if normalized and normalized.casefold() not in placeholders:
            meaningful_lines.append(normalized)
    return "\n".join(meaningful_lines)
