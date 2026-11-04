"""聊天管理页面专用的公开模型。"""

from __future__ import annotations

from datetime import UTC, datetime
from urllib.parse import quote

from pydantic import BaseModel, ConfigDict, Field, model_validator

from ..domain.models import (
    ConversationDirection,
    ConversationMessage,
    ConversationMessagePage,
    ConversationMessageType,
    ConversationQuote,
    ConversationStatus,
    ForwardingScheme,
    MessageOrigin,
    OutboundForwardingConfig,
    PublicApiTokenSnapshot,
)


class AdminVerificationCodeRequest(BaseModel):
    code: str = Field(min_length=1, max_length=32, pattern=r"^\d+$")


class PublicApiTokenResponse(BaseModel):
    token: str
    masked_token: str
    updated_at: datetime

    @classmethod
    def from_domain(cls, snapshot: PublicApiTokenSnapshot) -> PublicApiTokenResponse:
        return cls(
            token=snapshot.token,
            masked_token=f"{snapshot.token[:12]}…{snapshot.token[-4:]}",
            updated_at=snapshot.updated_at,
        )


class OutboundForwardingUpdateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    enabled: bool
    scheme: ForwardingScheme
    host: str = Field(min_length=1, max_length=253)
    port: int = Field(ge=1, le=65535)
    path: str = Field(min_length=1, max_length=512)
    secret: str | None = Field(default=None, min_length=32, max_length=512)
    timeout_seconds: float = Field(gt=0, le=60)
    max_attempts: int = Field(ge=1, le=10)

    @model_validator(mode="after")
    def validate_target(self) -> OutboundForwardingUpdateRequest:
        # 复用领域模型的主机、路径和组合校验；占位 Secret 不会被保存。
        OutboundForwardingConfig(
            enabled=self.enabled,
            scheme=self.scheme,
            host=self.host,
            port=self.port,
            path=self.path,
            secret=self.secret or "x" * 32,
            timeout_seconds=self.timeout_seconds,
            max_attempts=self.max_attempts,
            updated_at=datetime.now(UTC),
        )
        return self


class OutboundForwardingResponse(BaseModel):
    enabled: bool
    scheme: ForwardingScheme
    host: str
    port: int
    path: str
    target_url: str
    secret: str
    timeout_seconds: float
    max_attempts: int
    updated_at: datetime

    @classmethod
    def from_domain(cls, config: OutboundForwardingConfig) -> OutboundForwardingResponse:
        return cls(
            enabled=config.enabled,
            scheme=config.scheme,
            host=config.host,
            port=config.port,
            path=config.path,
            target_url=config.url,
            secret=config.secret,
            timeout_seconds=config.timeout_seconds,
            max_attempts=config.max_attempts,
            updated_at=config.updated_at,
        )


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
