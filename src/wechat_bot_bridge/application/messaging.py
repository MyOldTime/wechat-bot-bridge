"""负责同步出站投递与消息审计的应用服务。"""

from __future__ import annotations

import asyncio
import logging

from ..domain.errors import (
    BridgeError,
    GatewayNotReadyError,
    GatewayTemporaryError,
    MissingContextError,
    OwnerNotBoundError,
    QuotedMessageNotFoundError,
)
from ..domain.models import (
    ConversationQuote,
    MessageOrigin,
    OutboundMessage,
    OutboundPayload,
    OwnerBinding,
    TypingCancelPayload,
    TypingStartPayload,
    outbound_payload_type,
)
from ..domain.ports import (
    ConversationPublisher,
    OutboundMessageStore,
    WeChatGateway,
)
from .bridge_quotes import quote_from_message, with_bridge_quote
from .payloads import PayloadLimits, validate_outbound_payload

logger = logging.getLogger(__name__)


class NotificationService:
    """解析唯一 owner，并在当前 HTTP 请求内完成微信投递。"""

    def __init__(
        self,
        *,
        store: OutboundMessageStore,
        gateway: WeChatGateway,
        publisher: ConversationPublisher,
        message_max_chars: int,
        media_max_bytes: int,
        operation_lock: asyncio.Lock | None = None,
    ) -> None:
        self._store = store
        self._gateway = gateway
        self._publisher = publisher
        self._payload_limits = PayloadLimits(
            message_max_chars=message_max_chars,
            media_max_bytes=media_max_bytes,
        )
        # 发送和账号退出共用此锁，避免清理过程中夹入新的消息或媒体上传。
        self._operation_lock = operation_lock or asyncio.Lock()

    async def send(
        self,
        *,
        payload: OutboundPayload,
        origin: MessageOrigin,
        quote_message_id: str | None = None,
    ) -> OutboundMessage:
        validate_outbound_payload(payload, self._payload_limits)

        async with self._operation_lock:
            target = await self._resolve_delivery_target()
            quote = await self._resolve_quote(quote_message_id)
            delivery_payload = with_bridge_quote(payload, quote)
            validate_outbound_payload(delivery_payload, self._payload_limits)
            message = await self._store.begin_outbound(
                user_id=target.user_id,
                message_type=outbound_payload_type(payload),
                payload=payload,
                origin=origin,
                quote_message_id=quote_message_id,
            )
            await self._publish_stored_message(message.id)
            await self._deliver(
                message=message,
                payload=delivery_payload,
                user_id=target.user_id,
                context_token=target.context_token,
            )
            sent = await self._store.mark_outbound_sent(message.id)
            await self._publish_success(
                message=sent,
                payload=payload,
                origin=origin,
            )
            logger.info(
                "微信出站动作已完成",
                extra={
                    "message_id": sent.id,
                    "message_type": sent.message_type.value,
                    "origin": origin.value,
                    "delivery_status": sent.status.value,
                },
            )
            return sent

    async def _resolve_delivery_target(self) -> OwnerBinding:
        """解析与当前授权会话严格绑定的唯一 owner。"""

        fingerprint = (
            self._gateway.session_fingerprint or await self._store.get_active_fingerprint()
        )
        if not fingerprint:
            raise GatewayNotReadyError("No authorized WeChat session is available")
        target = await self._store.resolve_owner(fingerprint)
        if target is None:
            raise OwnerNotBoundError("The owner is not bound; send the bot a message first")
        if not target.context_token:
            raise MissingContextError("The owner must send the bot a new message first")
        return target

    async def _resolve_quote(
        self,
        message_id: str | None,
    ) -> ConversationQuote | None:
        if message_id is None:
            return None
        message = await self._store.get_conversation_message(message_id)
        if message is None:
            raise QuotedMessageNotFoundError("The quoted conversation message was not found")
        return quote_from_message(message)

    async def _deliver(
        self,
        *,
        message: OutboundMessage,
        payload: OutboundPayload,
        user_id: str,
        context_token: str,
    ) -> None:
        """执行一次上游调用，并保证失败状态不会遗留为 sending。"""

        try:
            await self._gateway.send_message(
                user_id=user_id,
                payload=payload,
                context_token=context_token,
            )
        except asyncio.CancelledError:
            await self._record_failure(message.id, "delivery_interrupted")
            raise
        except BridgeError as exc:
            await self._record_failure(message.id, exc.code)
            raise
        except Exception as exc:
            logger.exception(
                "微信出站动作发生未预期异常",
                extra={
                    "message_id": message.id,
                    "message_type": message.message_type.value,
                    "error_code": "internal_delivery_error",
                },
            )
            await self._record_failure(message.id, "internal_delivery_error")
            raise GatewayTemporaryError("An unexpected WeChat delivery error occurred") from exc

    async def _publish_success(
        self,
        *,
        message: OutboundMessage,
        payload: OutboundPayload,
        origin: MessageOrigin,
    ) -> None:
        if isinstance(payload, TypingStartPayload):
            await self._publisher.publish_typing(active=True, origin=origin)
        elif isinstance(payload, TypingCancelPayload):
            await self._publisher.publish_typing(active=False, origin=origin)
        else:
            await self._publish_stored_message(message.id)

    async def _record_failure(self, message_id: str, error_code: str) -> None:
        try:
            await self._store.mark_outbound_failed(message_id, error_code)
            await self._publish_stored_message(message_id)
        except Exception:
            # 审计写入失败不能覆盖更有价值的原始微信错误。
            logger.exception(
                "写入出站失败审计时发生异常",
                extra={"message_id": message_id, "error_code": error_code},
            )

    async def _publish_stored_message(self, message_id: str) -> None:
        message = await self._store.get_conversation_message(message_id)
        if message is not None:
            await self._publisher.publish_message(message)
