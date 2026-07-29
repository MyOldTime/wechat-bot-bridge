"""与业务领域隔离的微信 iLink 协议模型。"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import IntEnum, StrEnum
from typing import Literal


class WireMessageType(IntEnum):
    """iLink `message_type` 枚举。"""

    USER = 1
    BOT = 2


class WireMessageState(IntEnum):
    """iLink `message_state` 枚举。"""

    NEW = 0
    GENERATING = 1
    FINISH = 2


class WireItemType(IntEnum):
    """iLink `MessageItem.type` 枚举。"""

    TEXT = 1
    IMAGE = 2
    VOICE = 3
    FILE = 4
    VIDEO = 5


class UploadMediaType(IntEnum):
    """iLink CDN 上传媒体枚举。"""

    IMAGE = 1
    VIDEO = 2
    FILE = 3
    VOICE = 4


class TypingStatus(IntEnum):
    """机器人向微信发送的输入状态。"""

    TYPING = 1
    CANCEL = 2


class MediaKind(StrEnum):
    """客户端支持规范化的媒体类型。"""

    IMAGE = "image"
    VOICE = "voice"
    FILE = "file"
    VIDEO = "video"


type ContentType = Literal["text", "image", "voice", "file", "video"]


@dataclass(frozen=True, slots=True)
class Credentials:
    """扫码登录后由 iLink 返回并持久化的授权信息。"""

    token: str
    base_url: str
    account_id: str
    user_id: str
    saved_at: str | None = None


@dataclass(frozen=True, slots=True)
class CdnMedia:
    """iLink 消息中的 CDN 媒体引用。"""

    encrypted_query_param: str
    aes_key: str
    full_url: str | None = None
    encrypt_type: int | None = None


@dataclass(frozen=True, slots=True)
class MediaReference:
    """一条入站消息中可下载的媒体引用。"""

    kind: MediaKind
    cdn: CdnMedia
    aes_key_override: str | None = None
    file_name: str | None = None
    format: str | None = None


@dataclass(frozen=True, slots=True)
class QuotedMessage:
    """微信原生引用的一层入站快照。"""

    title: str | None
    text: str
    type: ContentType


@dataclass(frozen=True, slots=True)
class IncomingMessage:
    """从原始 iLink 字典解析后的可信内部消息。"""

    upstream_id: str
    user_id: str
    context_token: str
    text: str
    type: ContentType
    timestamp: datetime
    media: tuple[MediaReference, ...] = ()
    quoted_message: QuotedMessage | None = None


@dataclass(frozen=True, slots=True)
class UpdateBatch:
    """一次长轮询返回的游标和用户消息。"""

    cursor: str
    messages: tuple[IncomingMessage, ...]
    suggested_timeout_ms: int | None = None


@dataclass(frozen=True, slots=True)
class UploadedMedia:
    """上传到微信 CDN 后构造消息项所需的数据。"""

    encrypted_query_param: str
    aes_key: bytes
    plaintext_size: int
    ciphertext_size: int


@dataclass(frozen=True, slots=True)
class DownloadedMedia:
    """已下载并解密的入站媒体。"""

    data: bytes
    kind: MediaKind
    file_name: str | None = None
    format: str | None = None
