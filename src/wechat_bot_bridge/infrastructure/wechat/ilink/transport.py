"""基于 httpx 的 iLink HTTP 与 CDN 传输层。"""

from __future__ import annotations

import asyncio
import base64
import json
import os
import struct
from typing import Any
from urllib.parse import quote, urljoin, urlsplit

import httpx

from .constants import (
    BOT_AGENT,
    CHANNEL_VERSION,
    DEFAULT_CDN_BASE_URL,
    FIXED_QR_BASE_URL,
    ILINK_APP_CLIENT_VERSION,
    ILINK_APP_ID,
)
from .errors import (
    ILinkApiError,
    ILinkMediaError,
    ILinkNetworkError,
    ILinkProtocolError,
)
from .models import Credentials, TypingStatus, UploadMediaType

_API_TIMEOUT_SECONDS = 15.0
_CONFIG_TIMEOUT_SECONDS = 10.0
_LONG_POLL_TIMEOUT_SECONDS = 45.0
_QR_POLL_TIMEOUT_SECONDS = 35.0
_CDN_TIMEOUT_SECONDS = 60.0
_CDN_UPLOAD_ATTEMPTS = 3


class ILinkTransport:
    """与腾讯 iLink 端点一一对应的低层异步客户端。

    该类只负责认证头、超时、响应校验和字节传输，不持有 owner 或业务消息状态。
    """

    def __init__(
        self,
        *,
        http_client: httpx.AsyncClient | None = None,
        cdn_base_url: str = DEFAULT_CDN_BASE_URL,
    ) -> None:
        self._client = http_client or httpx.AsyncClient(
            follow_redirects=False,
            trust_env=True,
        )
        self._owns_client = http_client is None
        self._cdn_base_url = _validate_https_url(cdn_base_url, label="CDN base URL")

    @property
    def cdn_base_url(self) -> str:
        return self._cdn_base_url

    async def close(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    async def get_qr_code(self, *, local_tokens: list[str]) -> dict[str, Any]:
        return await self._request_json(
            method="POST",
            base_url=FIXED_QR_BASE_URL,
            endpoint="/ilink/bot/get_bot_qrcode",
            operation="get_bot_qrcode",
            params={"bot_type": "3"},
            body={"local_token_list": local_tokens[:10]},
            authenticated_headers=True,
            timeout_seconds=_API_TIMEOUT_SECONDS,
        )

    async def poll_qr_status(
        self,
        *,
        base_url: str,
        qrcode: str,
        verify_code: str | None = None,
    ) -> dict[str, Any]:
        params = {"qrcode": qrcode}
        if verify_code:
            params["verify_code"] = verify_code
        return await self._request_json(
            method="GET",
            base_url=base_url,
            endpoint="/ilink/bot/get_qrcode_status",
            operation="get_qrcode_status",
            params=params,
            timeout_seconds=_QR_POLL_TIMEOUT_SECONDS,
            read_timeout_as_empty={"status": "wait"},
        )

    async def get_updates(self, credentials: Credentials, cursor: str) -> dict[str, Any]:
        return await self._request_json(
            method="POST",
            base_url=credentials.base_url,
            endpoint="/ilink/bot/getupdates",
            operation="getupdates",
            token=credentials.token,
            body={
                "get_updates_buf": cursor,
                "base_info": _base_info(),
            },
            timeout_seconds=_LONG_POLL_TIMEOUT_SECONDS,
            read_timeout_as_empty={
                "ret": 0,
                "msgs": [],
                "get_updates_buf": cursor,
            },
        )

    async def send_message(
        self,
        credentials: Credentials,
        message: dict[str, Any],
    ) -> str | None:
        """发送消息并返回微信实际分配的消息 ID。

        腾讯公开的 TypeScript 类型暂未声明 ``message_id``，但线上
        ``sendmessage`` 成功响应会返回该整数；微信引用中的
        ``MessageItem.msg_id`` 使用同一个值。
        """

        payload = await self._request_json(
            method="POST",
            base_url=credentials.base_url,
            endpoint="/ilink/bot/sendmessage",
            operation="sendmessage",
            token=credentials.token,
            body={"msg": message, "base_info": _base_info()},
            timeout_seconds=_API_TIMEOUT_SECONDS,
        )
        return _response_message_id(payload)

    async def get_config(
        self,
        credentials: Credentials,
        *,
        user_id: str,
        context_token: str,
    ) -> dict[str, Any]:
        return await self._request_json(
            method="POST",
            base_url=credentials.base_url,
            endpoint="/ilink/bot/getconfig",
            operation="getconfig",
            token=credentials.token,
            body={
                "ilink_user_id": user_id,
                "context_token": context_token,
                "base_info": _base_info(),
            },
            timeout_seconds=_CONFIG_TIMEOUT_SECONDS,
        )

    async def send_typing(
        self,
        credentials: Credentials,
        *,
        user_id: str,
        typing_ticket: str,
        status: TypingStatus,
    ) -> None:
        await self._request_json(
            method="POST",
            base_url=credentials.base_url,
            endpoint="/ilink/bot/sendtyping",
            operation="sendtyping",
            token=credentials.token,
            body={
                "ilink_user_id": user_id,
                "typing_ticket": typing_ticket,
                "status": int(status),
                "base_info": _base_info(),
            },
            timeout_seconds=_CONFIG_TIMEOUT_SECONDS,
        )

    async def get_upload_url(
        self,
        credentials: Credentials,
        *,
        file_key: str,
        media_type: UploadMediaType,
        user_id: str,
        plaintext_size: int,
        plaintext_md5: str,
        ciphertext_size: int,
        aes_key_hex: str,
    ) -> dict[str, Any]:
        return await self._request_json(
            method="POST",
            base_url=credentials.base_url,
            endpoint="/ilink/bot/getuploadurl",
            operation="getuploadurl",
            token=credentials.token,
            body={
                "filekey": file_key,
                "media_type": int(media_type),
                "to_user_id": user_id,
                "rawsize": plaintext_size,
                "rawfilemd5": plaintext_md5,
                "filesize": ciphertext_size,
                "no_need_thumb": True,
                "aeskey": aes_key_hex,
                "base_info": _base_info(),
            },
            timeout_seconds=_API_TIMEOUT_SECONDS,
        )

    async def notify_start(self, credentials: Credentials) -> None:
        await self._notify(credentials, endpoint="/ilink/bot/msg/notifystart")

    async def notify_stop(self, credentials: Credentials) -> None:
        await self._notify(credentials, endpoint="/ilink/bot/msg/notifystop")

    async def _notify(self, credentials: Credentials, *, endpoint: str) -> None:
        await self._request_json(
            method="POST",
            base_url=credentials.base_url,
            endpoint=endpoint,
            operation=endpoint.rsplit("/", 1)[-1],
            token=credentials.token,
            body={"base_info": _base_info()},
            timeout_seconds=_CONFIG_TIMEOUT_SECONDS,
        )

    def build_cdn_upload_url(self, *, upload_param: str, file_key: str) -> str:
        return (
            f"{self._cdn_base_url.rstrip('/')}/upload"
            f"?encrypted_query_param={quote(upload_param, safe='')}"
            f"&filekey={quote(file_key, safe='')}"
        )

    def build_cdn_download_url(self, *, encrypted_query_param: str) -> str:
        return (
            f"{self._cdn_base_url.rstrip('/')}/download"
            f"?encrypted_query_param={quote(encrypted_query_param, safe='')}"
        )

    async def upload_cdn(self, *, url: str, ciphertext: bytes) -> str:
        """上传加密媒体；4xx 立即失败，网络错误和 5xx 最多尝试三次。"""

        target = _validate_https_url(url, label="CDN upload URL")
        last_error: ILinkMediaError | None = None
        timeout = _timeout(_CDN_TIMEOUT_SECONDS)
        for attempt in range(1, _CDN_UPLOAD_ATTEMPTS + 1):
            try:
                response = await self._client.post(
                    target,
                    content=ciphertext,
                    headers={"Content-Type": "application/octet-stream"},
                    timeout=timeout,
                )
            except httpx.RequestError as exc:
                last_error = ILinkMediaError("CDN upload network failure", temporary=True)
                if attempt == _CDN_UPLOAD_ATTEMPTS:
                    raise last_error from exc
            else:
                if 400 <= response.status_code < 500:
                    raise ILinkMediaError("CDN rejected the media upload")
                if response.status_code != 200:
                    last_error = ILinkMediaError(
                        "CDN temporarily rejected the media upload",
                        temporary=True,
                    )
                else:
                    encrypted_param = response.headers.get("x-encrypted-param")
                    if encrypted_param:
                        return encrypted_param
                    raise ILinkMediaError("CDN upload response is missing its media reference")
            if attempt < _CDN_UPLOAD_ATTEMPTS:
                await asyncio.sleep(0.25 * attempt)
        raise last_error or ILinkMediaError("CDN media upload failed", temporary=True)

    async def download_cdn(self, *, url: str, max_bytes: int) -> bytes:
        """流式读取 CDN 字节，并在解密前执行严格大小上限。"""

        target = _validate_https_url(url, label="CDN download URL")
        timeout = _timeout(_CDN_TIMEOUT_SECONDS)
        try:
            async with self._client.stream("GET", target, timeout=timeout) as response:
                if response.status_code >= 500:
                    raise ILinkMediaError(
                        "CDN temporarily rejected the media download",
                        temporary=True,
                    )
                if response.status_code >= 400:
                    raise ILinkMediaError("CDN rejected the media download")
                content_length = response.headers.get("content-length")
                if content_length is not None:
                    try:
                        declared_size = int(content_length)
                    except ValueError:
                        declared_size = 0
                    if declared_size > max_bytes:
                        raise ILinkMediaError("CDN media exceeds the configured size limit")
                chunks: list[bytes] = []
                received = 0
                async for chunk in response.aiter_bytes():
                    received += len(chunk)
                    if received > max_bytes:
                        raise ILinkMediaError("CDN media exceeds the configured size limit")
                    chunks.append(chunk)
                return b"".join(chunks)
        except ILinkMediaError:
            raise
        except httpx.RequestError as exc:
            raise ILinkMediaError("CDN download network failure", temporary=True) from exc

    async def _request_json(
        self,
        *,
        method: str,
        base_url: str,
        endpoint: str,
        operation: str,
        token: str | None = None,
        params: dict[str, str] | None = None,
        body: dict[str, Any] | None = None,
        authenticated_headers: bool = False,
        timeout_seconds: float,
        read_timeout_as_empty: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        base = _validate_https_url(base_url, label="iLink base URL")
        url = urljoin(f"{base.rstrip('/')}/", endpoint.lstrip("/"))
        headers = _common_headers()
        if method == "POST":
            headers["Content-Type"] = "application/json"
        if token or authenticated_headers:
            headers.update(_authorization_headers(token))
        try:
            response = await self._client.request(
                method,
                url,
                params=params,
                content=(
                    json.dumps(body, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
                    if body is not None
                    else None
                ),
                headers=headers,
                timeout=_timeout(timeout_seconds),
            )
        except httpx.ReadTimeout as exc:
            if read_timeout_as_empty is not None:
                return dict(read_timeout_as_empty)
            raise ILinkNetworkError(f"{operation} timed out") from exc
        except httpx.RequestError as exc:
            raise ILinkNetworkError(f"{operation} network failure") from exc

        payload = _decode_json_response(response, operation=operation)
        errcode = _response_error_code(payload)
        if response.status_code >= 400 or errcode != 0:
            raise ILinkApiError(
                operation,
                http_status=response.status_code,
                errcode=errcode,
            )
        return payload


def _base_info() -> dict[str, str]:
    return {
        "channel_version": CHANNEL_VERSION,
        "bot_agent": BOT_AGENT,
    }


def _common_headers() -> dict[str, str]:
    return {
        "iLink-App-Id": ILINK_APP_ID,
        "iLink-App-ClientVersion": ILINK_APP_CLIENT_VERSION,
    }


def _authorization_headers(token: str | None) -> dict[str, str]:
    headers = {
        "AuthorizationType": "ilink_bot_token",
        "X-WECHAT-UIN": _random_wechat_uin(),
    }
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return headers


def _random_wechat_uin() -> str:
    value = struct.unpack(">I", os.urandom(4))[0]
    return base64.b64encode(str(value).encode("ascii")).decode("ascii")


def _decode_json_response(
    response: httpx.Response,
    *,
    operation: str,
) -> dict[str, Any]:
    if not response.content:
        return {}
    try:
        payload = response.json()
    except ValueError as exc:
        if response.status_code >= 400:
            return {}
        raise ILinkProtocolError(f"{operation} returned invalid JSON") from exc
    if not isinstance(payload, dict):
        raise ILinkProtocolError(f"{operation} returned an invalid JSON object")
    return payload


def _response_error_code(payload: dict[str, Any]) -> int:
    errcode = payload.get("errcode")
    if isinstance(errcode, int) and not isinstance(errcode, bool) and errcode != 0:
        return errcode
    ret = payload.get("ret")
    if isinstance(ret, int) and not isinstance(ret, bool) and ret != 0:
        return ret
    return 0


def _response_message_id(payload: dict[str, Any]) -> str | None:
    """规范化线上响应的消息 ID；缺失时仍视为已成功发送。"""

    value = payload.get("message_id")
    if isinstance(value, bool) or not isinstance(value, (str, int)):
        return None
    normalized = str(value).strip()
    return normalized if 0 < len(normalized) <= 256 else None


def _validate_https_url(value: str, *, label: str) -> str:
    parsed = urlsplit(value)
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
    ):
        raise ILinkProtocolError(f"{label} must be an HTTPS URL without credentials")
    return value


def _timeout(total_seconds: float) -> httpx.Timeout:
    return httpx.Timeout(
        timeout=total_seconds,
        connect=min(total_seconds, 10.0),
        pool=min(total_seconds, 10.0),
    )
