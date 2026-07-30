"""独立于 FastAPI、SQLite 和 iLink 传输细节的内部领域模型。"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Final, Literal

from pydantic import BaseModel, ConfigDict, Field

OWNER_ALIAS: Final[str] = "owner"


class OutboundStatus(StrEnum):
    SENDING = "sending"
    SENT = "sent"
    FAILED = "failed"


class OutboundMessageType(StrEnum):
    TEXT = "text"
    IMAGE = "image"
    FILE = "file"
    TYPING_START = "typing_start"
    TYPING_CANCEL = "typing_cancel"


class MessageOrigin(StrEnum):
    EXTERNAL_API = "external_api"
    WEB_ADMIN = "web_admin"
    WECHAT = "wechat"


class ConversationDirection(StrEnum):
    INBOUND = "inbound"
    OUTBOUND = "outbound"


class ConversationStatus(StrEnum):
    RECEIVED = "received"
    SENDING = "sending"
    SENT = "sent"
    FAILED = "failed"


class ConversationMessageType(StrEnum):
    TEXT = "text"
    IMAGE = "image"
    VOICE = "voice"
    FILE = "file"
    VIDEO = "video"


@dataclass(frozen=True, slots=True)
class TextPayload:
    text: str


@dataclass(frozen=True, slots=True)
class ImagePayload:
    data: bytes
    caption: str | None = None


@dataclass(frozen=True, slots=True)
class FilePayload:
    data: bytes
    file_name: str
    caption: str | None = None


@dataclass(frozen=True, slots=True)
class TypingStartPayload:
    """显示微信的“正在输入”状态。"""


@dataclass(frozen=True, slots=True)
class TypingCancelPayload:
    """取消微信的“正在输入”状态。"""


type OutboundPayload = (
    TextPayload | ImagePayload | FilePayload | TypingStartPayload | TypingCancelPayload
)


def outbound_payload_type(payload: OutboundPayload) -> OutboundMessageType:
    """把内部载荷映射为稳定的审计消息类型。"""

    if isinstance(payload, TextPayload):
        return OutboundMessageType.TEXT
    if isinstance(payload, ImagePayload):
        return OutboundMessageType.IMAGE
    if isinstance(payload, FilePayload):
        return OutboundMessageType.FILE
    if isinstance(payload, TypingStartPayload):
        return OutboundMessageType.TYPING_START
    return OutboundMessageType.TYPING_CANCEL


@dataclass(frozen=True, slots=True)
class OwnerBinding:
    user_id: str
    context_token: str
    credential_fingerprint: str
    updated_at: datetime


@dataclass(frozen=True, slots=True)
class OutboundMessage:
    id: str
    user_id: str
    message_type: OutboundMessageType
    status: OutboundStatus
    last_error_code: str | None
    created_at: datetime
    updated_at: datetime
    sent_at: datetime | None
    # 只在当前同步发送响应中携带，不从 SQLite 恢复或持久化原始协议 ID。
    wechat_message_id: str | None = None


@dataclass(frozen=True, slots=True)
class WeChatDeliveryReceipt:
    """微信同步发送结果；原始 ID 仅供当前受认证调用方使用。"""

    wechat_message_id: str | None
    protocol_item_id: str | None


@dataclass(frozen=True, slots=True)
class ConversationQuote:
    """微信入站原生引用；目标 ID 仅在协议标识精确匹配时存在。"""

    target_message_id: str | None
    message_type: ConversationMessageType
    text: str
    title: str | None


@dataclass(frozen=True, slots=True)
class ConversationMessage:
    """管理页面使用的脱敏会话记录，不包含 owner 标识或上下文。"""

    id: str
    direction: ConversationDirection
    message_type: ConversationMessageType
    text: str
    file_name: str | None
    mime_type: str | None
    has_media: bool
    status: ConversationStatus
    origin: MessageOrigin
    error_code: str | None
    occurred_at: datetime
    updated_at: datetime
    quote: ConversationQuote | None


@dataclass(frozen=True, slots=True)
class ConversationMessagePage:
    """一页按时间正序返回的聊天记录及更早一页游标。"""

    items: tuple[ConversationMessage, ...]
    next_cursor: str | None


@dataclass(frozen=True, slots=True)
class ConversationMedia:
    data: bytes
    mime_type: str
    file_name: str | None


class InboundSender(BaseModel):
    model_config = ConfigDict(frozen=True)

    user_id: str


class InboundMessageBody(BaseModel):
    model_config = ConfigDict(frozen=True)

    type: Literal["text", "image", "voice", "file", "video"]
    text: str


class InboundQuote(BaseModel):
    """内置 iLink 客户端解析的微信原生引用快照。"""

    model_config = ConfigDict(frozen=True)

    title: str | None = None
    type: ConversationMessageType = ConversationMessageType.TEXT
    text: str
    protocol_item_id: str | None = Field(
        default=None,
        max_length=256,
        exclude=True,
        repr=False,
    )


class InboundMedia(BaseModel):
    """只供进程内 Sink 使用的媒体，不进入 Webhook 或去重信封。"""

    model_config = ConfigDict(frozen=True)

    data: bytes
    mime_type: str
    file_name: str | None = None


class InboundEvent(BaseModel):
    """已脱敏且带版本号，可安全交给下游出口的入站事件。"""

    model_config = ConfigDict(frozen=True)

    schema_version: Literal["1"] = "1"
    event_id: str
    source: Literal["wechat_ilink"] = "wechat_ilink"
    occurred_at: datetime
    sender: InboundSender
    message: InboundMessageBody
    quote: InboundQuote | None = None
    protocol_item_id: str | None = Field(
        default=None,
        max_length=256,
        exclude=True,
        repr=False,
    )
    # 微信入站引用可能用顶层 message_id 指向原消息，而不是原消息项的 msg_id。
    # 两个值均只保存账号隔离后的哈希，不进入 Webhook 或普通序列化结果。
    protocol_message_id: str | None = Field(
        default=None,
        max_length=256,
        exclude=True,
        repr=False,
    )
    media: InboundMedia | None = Field(default=None, exclude=True)


class GatewayPhase(StrEnum):
    STOPPED = "stopped"
    LOGIN_REQUIRED = "login_required"
    LOGGING_IN = "logging_in"
    LISTENING = "listening"
    ERROR = "error"


class GatewayStatus(BaseModel):
    model_config = ConfigDict(frozen=True)

    phase: GatewayPhase
    authenticated: bool
    listener_running: bool
    qr_ready: bool = False
    verification_required: bool = False
    error_code: str | None = None
