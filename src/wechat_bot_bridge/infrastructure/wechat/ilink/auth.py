"""二维码登录和 iLink 授权文件持久化。"""

from __future__ import annotations

import asyncio
import inspect
import json
import os
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol, TypeVar
from urllib.parse import urlsplit
from uuid import uuid4

from .constants import DEFAULT_API_BASE_URL, FIXED_QR_BASE_URL
from .errors import ILinkAuthError, ILinkProtocolError
from .models import Credentials

_QR_REFRESH_LIMIT = 3
_QR_POLL_INTERVAL_SECONDS = 1.0
_T = TypeVar("_T")


class LoginTransport(Protocol):
    async def get_qr_code(self, *, local_tokens: list[str]) -> dict[str, Any]: ...

    async def poll_qr_status(
        self,
        *,
        base_url: str,
        qrcode: str,
        verify_code: str | None = None,
    ) -> dict[str, Any]: ...


class CredentialStore:
    """兼容旧 SDK 文件格式的最小授权存储。"""

    def __init__(self, path: Path) -> None:
        self._path = path
        self._lock = asyncio.Lock()

    @property
    def path(self) -> Path:
        return self._path

    async def load(self) -> Credentials | None:
        async with self._lock:
            return await self._run_file_operation(self._load_sync)

    async def save(self, credentials: Credentials) -> None:
        async with self._lock:
            await self._run_file_operation(lambda: self._save_sync(credentials))

    async def clear(self) -> None:
        async with self._lock:
            await self._run_file_operation(lambda: self._path.unlink(missing_ok=True))

    async def _run_file_operation(self, operation: Callable[[], _T]) -> _T:
        """即使调用方取消，也等待文件线程结束，避免保存与清除发生反向竞态。"""

        task = asyncio.create_task(asyncio.to_thread(operation))
        try:
            return await asyncio.shield(task)
        except asyncio.CancelledError:
            await task
            raise

    def _load_sync(self) -> Credentials | None:
        try:
            payload = json.loads(self._path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return None
        except (OSError, json.JSONDecodeError) as exc:
            raise ILinkAuthError("The credentials file cannot be read") from exc
        if not isinstance(payload, dict):
            raise ILinkAuthError("The credentials file must contain a JSON object")

        token = _required_text(payload, "token")
        account_id = _required_text(payload, "account_id", "accountId")
        user_id = _required_text(payload, "user_id", "userId")
        base_url = _optional_text(payload, "base_url", "baseUrl") or DEFAULT_API_BASE_URL
        saved_at = _optional_text(payload, "saved_at", "savedAt")
        _validate_server_base_url(base_url)
        return Credentials(
            token=token,
            base_url=base_url,
            account_id=account_id,
            user_id=user_id,
            saved_at=saved_at,
        )

    def _save_sync(self, credentials: Credentials) -> None:
        _validate_server_base_url(credentials.base_url)
        self._path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        payload = {
            "token": credentials.token,
            "baseUrl": credentials.base_url,
            "accountId": credentials.account_id,
            "userId": credentials.user_id,
            "savedAt": credentials.saved_at,
        }
        temporary = self._path.with_name(f".{self._path.name}.{uuid4().hex}.tmp")
        try:
            temporary.write_text(
                json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
            temporary.chmod(0o600)
            os.replace(temporary, self._path)
            self._path.chmod(0o600)
        finally:
            temporary.unlink(missing_ok=True)


class QRAuthenticator:
    """执行 iLink 扫码、配对码和 IDC 跳转登录流程。"""

    def __init__(
        self,
        *,
        transport: LoginTransport,
        credentials: CredentialStore,
        on_qr_url: Callable[[str], None] | None = None,
        on_scanned: Callable[[], None] | None = None,
        on_expired: Callable[[], None] | None = None,
        on_verify_code: Callable[[bool], str | Awaitable[str]] | None = None,
    ) -> None:
        self._transport = transport
        self._credentials = credentials
        self._on_qr_url = on_qr_url
        self._on_scanned = on_scanned
        self._on_expired = on_expired
        self._on_verify_code = on_verify_code

    async def authorize(self, *, force: bool = False) -> Credentials:
        stored = await self._credentials.load()
        if stored is not None and not force:
            return stored

        local_tokens = [stored.token] if stored is not None else []
        for _refresh in range(_QR_REFRESH_LIMIT):
            qr_payload = await self._transport.get_qr_code(local_tokens=local_tokens)
            qrcode = _required_text(qr_payload, "qrcode")
            qr_url = _required_text(qr_payload, "qrcode_img_content")
            if self._on_qr_url is not None:
                self._on_qr_url(qr_url)

            polling_base_url = FIXED_QR_BASE_URL
            pending_verify_code: str | None = None
            last_status: str | None = None
            while True:
                status_payload = await self._transport.poll_qr_status(
                    base_url=polling_base_url,
                    qrcode=qrcode,
                    verify_code=pending_verify_code,
                )
                status = _required_text(status_payload, "status")
                if status == "confirmed":
                    credentials = _credentials_from_confirmation(status_payload)
                    await self._credentials.save(credentials)
                    return credentials
                if status == "binded_redirect":
                    if stored is not None:
                        return stored
                    raise ILinkAuthError(
                        "The bot is already bound but no local credentials are available"
                    )
                if status == "scaned_but_redirect":
                    redirect_host = _required_text(status_payload, "redirect_host")
                    polling_base_url = _redirect_base_url(redirect_host)
                elif status == "need_verifycode":
                    pending_verify_code = await self._read_verify_code(
                        retry=pending_verify_code is not None
                    )
                    continue
                elif status == "verify_code_blocked":
                    if self._on_expired is not None:
                        self._on_expired()
                    break
                elif status == "expired":
                    if self._on_expired is not None:
                        self._on_expired()
                    break
                elif status == "scaned":
                    if pending_verify_code is not None:
                        pending_verify_code = None
                    if status != last_status and self._on_scanned is not None:
                        self._on_scanned()
                elif status != "wait":
                    raise ILinkProtocolError("get_qrcode_status returned an unknown status")
                last_status = status
                await asyncio.sleep(_QR_POLL_INTERVAL_SECONDS)

        raise ILinkAuthError("The QR code expired too many times")

    async def _read_verify_code(self, *, retry: bool) -> str:
        if self._on_verify_code is None:
            # 常驻服务不能阻塞容器终端；配对数字必须来自受保护的管理页面。
            raise ILinkAuthError("No management verification code provider is configured")
        provided = self._on_verify_code(retry)
        value = await provided if inspect.isawaitable(provided) else provided
        normalized = value.strip()
        if not normalized:
            raise ILinkAuthError("The pairing code cannot be empty")
        return normalized


def _credentials_from_confirmation(payload: dict[str, Any]) -> Credentials:
    token = _required_text(payload, "bot_token")
    account_id = _required_text(payload, "ilink_bot_id")
    user_id = _required_text(payload, "ilink_user_id")
    base_url = _optional_text(payload, "baseurl") or DEFAULT_API_BASE_URL
    _validate_server_base_url(base_url)
    return Credentials(
        token=token,
        base_url=base_url,
        account_id=account_id,
        user_id=user_id,
        saved_at=datetime.now(UTC).isoformat(),
    )


def _required_text(payload: dict[str, Any], *keys: str) -> str:
    value = _optional_text(payload, *keys)
    if value is None:
        raise ILinkAuthError("The iLink login response is incomplete")
    return value


def _optional_text(payload: dict[str, Any], *keys: str) -> str | None:
    for key in keys:
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def _redirect_base_url(host: str) -> str:
    if "/" in host or "@" in host or ":" in host:
        raise ILinkAuthError("The iLink redirect host is invalid")
    value = f"https://{host}"
    _validate_server_base_url(value)
    return value


def _validate_server_base_url(value: str) -> None:
    parsed = urlsplit(value)
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
    ):
        raise ILinkAuthError("The iLink base URL must use HTTPS")
