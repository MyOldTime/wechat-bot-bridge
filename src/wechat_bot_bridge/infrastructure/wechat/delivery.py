"""领域出站载荷到 iLink 高层客户端调用的映射。"""

from __future__ import annotations

from typing import Protocol

from ...domain.models import (
    FilePayload,
    ImagePayload,
    OutboundPayload,
    TextPayload,
    TypingCancelPayload,
    TypingStartPayload,
)


class OutboundClient(Protocol):
    """投递映射所需的最小客户端能力。"""

    async def send_text(
        self,
        *,
        user_id: str,
        context_token: str,
        text: str,
    ) -> None: ...

    async def send_image(
        self,
        *,
        user_id: str,
        context_token: str,
        data: bytes,
        caption: str | None,
    ) -> None: ...

    async def send_file(
        self,
        *,
        user_id: str,
        context_token: str,
        data: bytes,
        file_name: str,
        caption: str | None,
    ) -> None: ...

    async def set_typing(
        self,
        *,
        user_id: str,
        context_token: str,
        active: bool,
    ) -> None: ...


async def deliver_outbound_payload(
    client: OutboundClient,
    *,
    user_id: str,
    context_token: str,
    payload: OutboundPayload,
) -> None:
    """穷举当前公开的领域载荷，并调用对应 iLink 能力。"""

    if isinstance(payload, TextPayload):
        await client.send_text(
            user_id=user_id,
            context_token=context_token,
            text=payload.text,
        )
        return
    if isinstance(payload, ImagePayload):
        await client.send_image(
            user_id=user_id,
            context_token=context_token,
            data=payload.data,
            caption=payload.caption,
        )
        return
    if isinstance(payload, FilePayload):
        await client.send_file(
            user_id=user_id,
            context_token=context_token,
            data=payload.data,
            file_name=payload.file_name,
            caption=payload.caption,
        )
        return
    if isinstance(payload, TypingStartPayload):
        await client.set_typing(
            user_id=user_id,
            context_token=context_token,
            active=True,
        )
        return
    if isinstance(payload, TypingCancelPayload):
        await client.set_typing(
            user_id=user_id,
            context_token=context_token,
            active=False,
        )
        return
    raise TypeError(f"Unsupported outbound payload: {type(payload).__name__}")
