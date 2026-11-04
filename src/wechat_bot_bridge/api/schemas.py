"""MCP 工具与管理页面共用的发送模型和领域转换。"""

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
            "contentEncoding": "base64",
            "description": (
                "必填。将图片或文件的原始字节编码为标准 Base64 字符串。"
                "只填编码内容，不加换行或 data: URL 前缀。解码后至少 1 字节，"
                "最大字节数见工具说明中的当前部署上限。"
            ),
        }
    ),
]


class _MessageRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=False)


class TextMessageRequest(_MessageRequest):
    type: Literal["text"] = Field(description="固定为 text，发送文字通知，必须同时填写 text。")
    text: str = Field(
        min_length=1,
        max_length=100_000,
        description=(
            "必填正文，至少含一个非空白字符。建议用 Markdown 的短标题、换行和列表组织多行通知，"
            "单句通知也可直接写纯文本。桥接服务按微信文本发送，保留 Markdown 标记。"
            "总长度受部署文字上限约束，超过 4000 字符时分条发送。"
        ),
        examples=["## 构建完成\n- 版本：v1.2.0\n- 测试：通过"],
    )


_CAPTION_DESCRIPTION = (
    "可选说明文字。省略或传 null 表示只发送媒体。填写时须含非空白字符，"
    "最多 4000 字符，同时受部署文字上限约束。说明先作为单独的文字消息发送。"
)


class ImageMessageRequest(_MessageRequest):
    type: Literal["image"] = Field(
        description="固定为 image，发送微信图片。必填 content_base64，可选 caption。"
    )
    content_base64: Base64Media
    caption: str | None = Field(
        default=None,
        min_length=1,
        max_length=4_000,
        description=_CAPTION_DESCRIPTION,
    )


class FileMessageRequest(_MessageRequest):
    type: Literal["file"] = Field(
        description="固定为 file，发送普通文件。必填 content_base64 和 file_name，可选 caption。"
    )
    content_base64: Base64Media
    file_name: str = Field(
        min_length=1,
        max_length=255,
        description=(
            "收件人看到的文件名，必填，最多 255 字符。不能仅含空白，不能含路径分隔符或 NUL。"
            f"图片扩展名 {'、'.join(sorted(_IMAGE_EXTENSIONS))} 须改用 image。"
            f"不支持视频扩展名 {'、'.join(sorted(_VIDEO_EXTENSIONS))}。"
        ),
        examples=["report.pdf"],
    )
    caption: str | None = Field(
        default=None,
        min_length=1,
        max_length=4_000,
        description=_CAPTION_DESCRIPTION,
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
    type: Literal["typing_start"] = Field(
        description="固定为 typing_start，显示“正在输入”。只传 type，结束时调用 typing_cancel。"
    )


class TypingCancelRequest(_MessageRequest):
    type: Literal["typing_cancel"] = Field(
        description="固定为 typing_cancel，取消“正在输入”提示。只传 type，不产生聊天气泡。"
    )


type SendMessageRequest = Annotated[
    TextMessageRequest
    | ImageMessageRequest
    | FileMessageRequest
    | TypingStartRequest
    | TypingCancelRequest,
    Field(discriminator="type"),
]


class SendToolArguments(BaseModel):
    model_config = ConfigDict(extra="forbid")

    message: SendMessageRequest = Field(
        description=(
            "必填对象。向当前绑定的唯一微信账号发送一个动作。用 type 选择 text、image、file、"
            "typing_start 或 typing_cancel，只填写该类型的字段，无需指定收件人。"
        )
    )


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

    id: str = Field(description="本次发送的本地审计 UUID。重复发送相同内容会取得新的 id。")
    msg_id: str | None = Field(
        default=None,
        description=(
            "微信返回的主消息 ID，以字符串表示。长文字取第一段，带说明的媒体取图片或文件本身。"
            "输入状态或上游未返回 ID 时为 null。"
        ),
    )
    message_type: OutboundMessageType = Field(
        serialization_alias="type",
        description=(
            "已执行的动作，与请求的 message.type 相同。"
            "取值为 text、image、file、typing_start 或 typing_cancel。"
        ),
    )
    status: Literal["sent"] = Field(
        description="成功回执固定为 sent，表示微信 iLink 已接受该动作，不代表对方已读。"
    )
    created_at: datetime = Field(description="本地创建审计记录的时间，使用 UTC 的 RFC 3339 格式。")
    sent_at: datetime = Field(
        description="iLink 接受动作后本地记录成功的时间，使用 UTC 的 RFC 3339 格式。"
    )

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
