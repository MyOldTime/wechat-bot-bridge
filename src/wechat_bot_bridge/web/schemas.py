"""聊天管理页面专用的公开模型。"""

from __future__ import annotations

from datetime import datetime
from urllib.parse import quote

from pydantic import BaseModel, ConfigDict, Field

from ..domain.models import (
    ConversationDirection,
    ConversationMessage,
    ConversationMessagePage,
    ConversationMessageType,
    ConversationQuote,
    ConversationStatus,
    MessageOrigin,
)


class AdminVerificationCodeRequest(BaseModel):
    code: str = Field(min_length=1, max_length=32, pattern=r"^\d+$")


class InboundQuoteResponse(BaseModel):
    """管理页面可见的微信原生引用快照。"""

    model_config = ConfigDict(populate_by_name=True)

    message_id: str | None = None
    message_type: ConversationMessageType = Field(serialization_alias="type")
    text: str
    title: str | None = None

    @classmethod
    def from_domain(cls, quoted: ConversationQuote) -> InboundQuoteResponse:
        return cls(
            message_id=quoted.target_message_id,
            message_type=quoted.message_type,
            text=quoted.text,
            title=quoted.title,
        )


class ConversationMessageResponse(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    id: str
    direction: ConversationDirection
    message_type: ConversationMessageType = Field(serialization_alias="type")
    text: str
    file_name: str | None = None
    mime_type: str | None = None
    media_url: str | None = None
    status: ConversationStatus
    origin: MessageOrigin
    error_code: str | None = None
    occurred_at: datetime
    updated_at: datetime
    quote: InboundQuoteResponse | None = None

    @classmethod
    def from_domain(
        cls,
        message: ConversationMessage,
    ) -> ConversationMessageResponse:
        media_url = (
            f"/admin/api/messages/{quote(message.id, safe='')}/media" if message.has_media else None
        )
        return cls(
            id=message.id,
            direction=message.direction,
            message_type=message.message_type,
            text=message.text,
            file_name=message.file_name,
            mime_type=message.mime_type,
            media_url=media_url,
            status=message.status,
            origin=message.origin,
            error_code=message.error_code,
            occurred_at=message.occurred_at,
            updated_at=message.updated_at,
            quote=(
                InboundQuoteResponse.from_domain(message.quote)
                if message.quote is not None
                else None
            ),
        )


class ConversationMessagePageResponse(BaseModel):
    items: list[ConversationMessageResponse]
    next_cursor: str | None = None

    @classmethod
    def from_domain(
        cls,
        page: ConversationMessagePage,
    ) -> ConversationMessagePageResponse:
        return cls(
            items=[ConversationMessageResponse.from_domain(message) for message in page.items],
            next_cursor=page.next_cursor,
        )
