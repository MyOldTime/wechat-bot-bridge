"""项目内置的微信 iLink 高层客户端。"""

from __future__ import annotations

from .media import MediaTransfer, encode_uploaded_aes_key
from .messages import (
    build_file_item,
    build_image_item,
    build_message,
    build_text_item,
    chunk_text,
    parse_update_batch,
)
from .models import (
    Credentials,
    DownloadedMedia,
    IncomingMessage,
    TypingStatus,
    UpdateBatch,
    UploadMediaType,
)
from .transport import ILinkTransport


class ILinkClient:
    """组合协议传输、消息模型和 CDN，向网关提供稳定 Python API。"""

    def __init__(
        self,
        *,
        credentials: Credentials,
        transport: ILinkTransport,
        media_max_bytes: int,
    ) -> None:
        self._credentials = credentials
        self._transport = transport
        self._media = MediaTransfer(transport)
        self._media_max_bytes = media_max_bytes

    @property
    def credentials(self) -> Credentials:
        return self._credentials

    async def get_updates(self, cursor: str) -> UpdateBatch:
        payload = await self._transport.get_updates(self._credentials, cursor)
        return parse_update_batch(payload, current_cursor=cursor)

    async def send_text(
        self,
        *,
        message_id: str,
        user_id: str,
        context_token: str,
        text: str,
    ) -> str | None:
        primary_message_id: str | None = None
        for index, chunk in enumerate(chunk_text(text)):
            # 每个协议请求必须有唯一 ID；首段沿用本地消息 ID，便于引用主消息。
            chunk_message_id = message_id if index == 0 else f"{message_id}:part:{index + 1}"
            server_message_id = await self._transport.send_message(
                self._credentials,
                build_message(
                    message_id=chunk_message_id,
                    user_id=user_id,
                    context_token=context_token,
                    item=build_text_item(chunk),
                ),
            )
            if index == 0:
                primary_message_id = server_message_id
        return primary_message_id

    async def send_image(
        self,
        *,
        message_id: str,
        user_id: str,
        context_token: str,
        data: bytes,
        caption: str | None,
    ) -> str | None:
        if caption:
            await self.send_text(
                message_id=f"{message_id}:caption",
                user_id=user_id,
                context_token=context_token,
                text=caption,
            )
        uploaded = await self._media.upload(
            credentials=self._credentials,
            user_id=user_id,
            data=data,
            media_type=UploadMediaType.IMAGE,
        )
        return await self._transport.send_message(
            self._credentials,
            build_message(
                message_id=message_id,
                user_id=user_id,
                context_token=context_token,
                item=build_image_item(
                    encrypted_query_param=uploaded.encrypted_query_param,
                    aes_key=encode_uploaded_aes_key(uploaded),
                    ciphertext_size=uploaded.ciphertext_size,
                ),
            ),
        )

    async def send_file(
        self,
        *,
        message_id: str,
        user_id: str,
        context_token: str,
        data: bytes,
        file_name: str,
        caption: str | None,
    ) -> str | None:
        if caption:
            await self.send_text(
                message_id=f"{message_id}:caption",
                user_id=user_id,
                context_token=context_token,
                text=caption,
            )
        uploaded = await self._media.upload(
            credentials=self._credentials,
            user_id=user_id,
            data=data,
            media_type=UploadMediaType.FILE,
        )
        return await self._transport.send_message(
            self._credentials,
            build_message(
                message_id=message_id,
                user_id=user_id,
                context_token=context_token,
                item=build_file_item(
                    encrypted_query_param=uploaded.encrypted_query_param,
                    aes_key=encode_uploaded_aes_key(uploaded),
                    plaintext_size=uploaded.plaintext_size,
                    file_name=file_name,
                ),
            ),
        )

    async def set_typing(
        self,
        *,
        user_id: str,
        context_token: str,
        active: bool,
    ) -> None:
        config = await self._transport.get_config(
            self._credentials,
            user_id=user_id,
            context_token=context_token,
        )
        ticket = config.get("typing_ticket")
        if not isinstance(ticket, str) or not ticket:
            return
        await self._transport.send_typing(
            self._credentials,
            user_id=user_id,
            typing_ticket=ticket,
            status=TypingStatus.TYPING if active else TypingStatus.CANCEL,
        )

    async def download(self, message: IncomingMessage) -> DownloadedMedia | None:
        if not message.media:
            return None
        return await self._media.download(
            message.media[0],
            max_plaintext_bytes=self._media_max_bytes,
        )

    async def notify_start(self) -> None:
        await self._transport.notify_start(self._credentials)

    async def notify_stop(self) -> None:
        await self._transport.notify_stop(self._credentials)
