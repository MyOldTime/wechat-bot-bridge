"""可替换的基础设施端口定义。"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Protocol

from .models import (
    ConversationMedia,
    ConversationMessage,
    ConversationMessagePage,
    GatewayStatus,
    InboundEvent,
    MessageOrigin,
    OutboundForwardingConfig,
    OutboundMessage,
    OutboundMessageType,
    OutboundPayload,
    OwnerBinding,
    WeChatDeliveryReceipt,
)

InboundHandler = Callable[[InboundEvent], Awaitable[None]]


class WeChatGateway(Protocol):
    @property
    def status(self) -> GatewayStatus: ...

    @property
    def session_fingerprint(self) -> str | None: ...

    @property
    def login_qr_url(self) -> str | None: ...

    async def run(self) -> None: ...

    async def stop(self) -> None: ...

    async def start_login(self) -> None: ...

    async def submit_verification_code(self, code: str) -> None: ...

    async def logout(self) -> None: ...

    async def send_message(
        self,
        *,
        message_id: str,
        user_id: str,
        payload: OutboundPayload,
        context_token: str,
    ) -> WeChatDeliveryReceipt: ...


class InboundSink(Protocol):
    async def deliver(self, event: InboundEvent) -> None: ...

    async def close(self) -> None: ...


class InboundPublisher(Protocol):
    async def publish(self, event: InboundEvent) -> None: ...

    async def drain(self) -> None: ...


class ConversationPublisher(Protocol):
    async def publish_message(self, message: ConversationMessage) -> None: ...

    async def publish_typing(
        self,
        *,
        active: bool,
        origin: MessageOrigin,
    ) -> None: ...

    async def publish_reset(self) -> None: ...


class OutboundMessageStore(Protocol):
    async def get_active_fingerprint(self) -> str | None: ...

    async def resolve_owner(self, credential_fingerprint: str) -> OwnerBinding | None: ...

    async def begin_outbound(
        self,
        *,
        user_id: str,
        message_type: OutboundMessageType,
        payload: OutboundPayload,
        origin: MessageOrigin,
    ) -> OutboundMessage: ...

    async def mark_outbound_sent(
        self,
        message_id: str,
        *,
        protocol_item_id: str | None = None,
    ) -> OutboundMessage: ...

    async def mark_outbound_failed(self, message_id: str, error_code: str) -> OutboundMessage: ...

    async def get_conversation_message(
        self,
        message_id: str,
    ) -> ConversationMessage | None: ...

    async def list_conversation_messages(
        self,
        *,
        cursor: str | None,
        limit: int,
    ) -> ConversationMessagePage: ...

    async def get_conversation_media(
        self,
        message_id: str,
    ) -> ConversationMedia | None: ...


class WeChatStateStore(Protocol):
    async def activate_session(self, fingerprint: str) -> bool: ...

    async def reconcile_owner_binding(
        self,
        *,
        user_id: str,
        credential_fingerprint: str,
    ) -> bool: ...

    async def save_owner_context(
        self,
        *,
        user_id: str,
        context_token: str,
        credential_fingerprint: str,
    ) -> None: ...

    async def remember_inbound_event(self, event: InboundEvent) -> bool: ...

    async def record_inbound_conversation(
        self,
        event: InboundEvent,
    ) -> ConversationMessage: ...

    async def get_state(self, key: str) -> str | None: ...

    async def set_state(self, key: str, value: str) -> None: ...


class ServiceConfigurationStore(Protocol):
    """不随微信账号退出而清除的服务级配置存储。"""

    async def get_service_setting(self, key: str) -> str | None: ...

    async def set_service_setting(self, key: str, value: str) -> None: ...


class OutboundForwardingConfigProvider(Protocol):
    async def current(self) -> OutboundForwardingConfig: ...


class BridgeStore(
    OutboundMessageStore,
    WeChatStateStore,
    ServiceConfigurationStore,
    Protocol,
):
    @property
    def is_open(self) -> bool: ...

    async def open(self) -> None: ...

    async def clear_all(self) -> None: ...

    async def close(self) -> None: ...
