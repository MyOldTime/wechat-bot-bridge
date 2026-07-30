"""HTTP API 专用的请求、响应与领域转换模型。"""

from __future__ import annotations

import base64
import binascii
from datetime import datetime
from pathlib import Path
from typing import Annotated, Any, Literal

from pydantic import (
    BaseModel,
    BeforeValidator,
    ConfigDict,
    Field,
    WithJsonSchema,
    field_validator,
)

from ..domain.models import (
    FilePayload,
    ImagePayload,
    OutboundMessage,
    OutboundMessageType,
    OutboundPayload,
    OutboundStatus,
    TextPayload,
    TypingCancelPayload,
    TypingStartPayload,
)

# 请求级硬上限用于在进入业务层前限制内存占用；更小的部署上限由配置控制。
_ABSOLUTE_MEDIA_BYTES = 25 * 1024 * 1024
_IMAGE_EXTENSIONS = {".bmp", ".gif", ".jpeg", ".jpg", ".png", ".svg", ".webp"}
_VIDEO_EXTENSIONS = {".avi", ".mkv", ".mov", ".mp4", ".webm"}


def _decode_base64(value: Any) -> bytes:
    if not isinstance(value, str):
        raise ValueError("content_base64 must be a Base64 string")
    try:
        encoded = value.encode("ascii")
        return base64.b64decode(encoded, validate=True)
    except (UnicodeEncodeError, binascii.Error, ValueError) as exc:
        raise ValueError("content_base64 is not valid standard Base64") from exc


Base64Media = Annotated[
    bytes,
    BeforeValidator(_decode_base64),
    Field(min_length=1, max_length=_ABSOLUTE_MEDIA_BYTES),
    WithJsonSchema(
        {
            "type": "string",
            "format": "byte",
            "description": "标准 Base64 编码的二进制内容，不含 data: URL 前缀",
        }
    ),
]


class _MessageRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=False)


class TextMessageRequest(_MessageRequest):
    type: Literal["text"]
    text: str = Field(min_length=1, max_length=100_000, description="要发送的文字")


class ImageMessageRequest(_MessageRequest):
    type: Literal["image"]
    content_base64: Base64Media
    caption: str | None = Field(
        default=None,
        min_length=1,
        max_length=4_000,
        description="可选的图片说明",
    )


class FileMessageRequest(_MessageRequest):
    type: Literal["file"]
    content_base64: Base64Media
    file_name: str = Field(
        min_length=1,
        max_length=255,
        description="收件人在微信中看到的文件名",
    )
    caption: str | None = Field(
        default=None,
        min_length=1,
        max_length=4_000,
        description="可选的文件说明",
    )

    @field_validator("file_name")
    @classmethod
    def validate_file_name(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("file_name cannot be blank")
        if any(character in normalized for character in ("/", "\\", "\0")):
            raise ValueError("file_name must be a plain file name without a path")
        suffix = Path(normalized).suffix.lower()
        if suffix in _IMAGE_EXTENSIONS:
            raise ValueError("Image file extensions must use type=image")
        if suffix in _VIDEO_EXTENSIONS:
            raise ValueError("Video sending is not supported by this API")
        return normalized


class TypingStartRequest(_MessageRequest):
    type: Literal["typing_start"]


class TypingCancelRequest(_MessageRequest):
    type: Literal["typing_cancel"]


type SendMessageRequest = Annotated[
    TextMessageRequest
    | ImageMessageRequest
    | FileMessageRequest
    | TypingStartRequest
    | TypingCancelRequest,
    Field(discriminator="type"),
]


def to_domain_payload(request: SendMessageRequest) -> OutboundPayload:
    """把经过 HTTP 校验的显式消息类型转换为内部载荷。"""

    if isinstance(request, TextMessageRequest):
        return TextPayload(text=request.text)
    if isinstance(request, ImageMessageRequest):
        return ImagePayload(data=request.content_base64, caption=request.caption)
    if isinstance(request, FileMessageRequest):
        return FilePayload(
            data=request.content_base64,
            file_name=request.file_name,
            caption=request.caption,
        )
    if isinstance(request, TypingStartRequest):
        return TypingStartPayload()
    return TypingCancelPayload()


class SendMessageResponse(BaseModel):
    """发送接口只在微信 iLink 已同步接受消息后返回。"""

    model_config = ConfigDict(populate_by_name=True)

    id: str
    msg_id: str | None = Field(
        default=None,
        description=("微信服务端为主消息分配的 ID；输入状态或上游未返回 ID 时为 null"),
    )
    message_type: OutboundMessageType = Field(serialization_alias="type")
    status: Literal["sent"]
    created_at: datetime
    sent_at: datetime

    @classmethod
    def from_domain(cls, message: OutboundMessage) -> SendMessageResponse:
        if message.status is not OutboundStatus.SENT or message.sent_at is None:
            raise ValueError("Only a sent message can be returned as a success response")
        return cls(
            id=message.id,
            msg_id=message.wechat_message_id,
            message_type=message.message_type,
            status="sent",
            created_at=message.created_at,
            sent_at=message.sent_at,
        )
