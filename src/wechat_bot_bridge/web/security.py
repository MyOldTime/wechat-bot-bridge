"""管理页面的 Basic Auth 与短期同源令牌。"""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import secrets
import time
from typing import Annotated
from urllib.parse import parse_qs

from fastapi import HTTPException, Request, Security, status
from fastapi.security import HTTPBasic, HTTPBasicCredentials

from ..api.errors import http_error

basic_auth = HTTPBasic(auto_error=False)


def require_admin_token(request: Request, token: str) -> None:
    """校验已经通过 Basic Auth 页面签发的短期写操作令牌。"""

    signer: AdminTokenSigner = request.app.state.admin_token_signer
    if not signer.verify(token):
        raise http_error(403, "invalid_admin_token", "The management token is invalid or expired")


async def admin_token_from_form(request: Request) -> str:
    """只解析很小的 URL 编码退出表单，避免为单字段引入 multipart 依赖。"""

    content_type = request.headers.get("content-type", "").partition(";")[0].strip().lower()
    if content_type != "application/x-www-form-urlencoded":
        raise http_error(415, "invalid_form", "The logout form content type is invalid")
    body = await request.body()
    if len(body) > 4096:
        raise http_error(413, "form_too_large", "The logout form is too large")
    try:
        fields = parse_qs(
            body.decode("utf-8"),
            keep_blank_values=True,
            strict_parsing=True,
            max_num_fields=4,
        )
    except (UnicodeDecodeError, ValueError) as exc:
        raise http_error(422, "invalid_form", "The logout form is invalid") from exc
    tokens = fields.get("admin_token", [])
    if len(tokens) != 1:
        raise http_error(422, "invalid_form", "The logout form token is missing")
    return tokens[0]


async def require_web_basic_auth(
    request: Request,
    credentials: Annotated[HTTPBasicCredentials | None, Security(basic_auth)],
) -> str:
    settings = request.app.state.settings
    configured_password = settings.web_password
    if configured_password is None:
        raise http_error(
            503,
            "web_admin_disabled",
            "Set WBB_WEB_PASSWORD to enable the management page",
        )

    provided_username = credentials.username if credentials is not None else ""
    provided_password = credentials.password if credentials is not None else ""
    username_valid = secrets.compare_digest(provided_username, settings.web_username)
    password_valid = secrets.compare_digest(
        provided_password,
        configured_password.get_secret_value(),
    )
    if not (username_valid and password_valid):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={"code": "unauthorized", "message": "Invalid management credentials"},
            headers={"WWW-Authenticate": 'Basic realm="wechat-bot-bridge", charset="UTF-8"'},
        )
    return settings.web_username


class AdminTokenSigner:
    """为 WebSocket 与管理端写操作签发短期防跨站令牌。"""

    def __init__(self, *, ttl_seconds: int, secret: bytes | None = None) -> None:
        self._ttl_seconds = ttl_seconds
        self._secret = secret or secrets.token_bytes(32)

    def issue(self) -> str:
        expires_at = int(time.time()) + self._ttl_seconds
        payload = f"{expires_at}:{secrets.token_urlsafe(18)}".encode("ascii")
        encoded = base64.urlsafe_b64encode(payload).decode("ascii").rstrip("=")
        signature = hmac.new(self._secret, encoded.encode("ascii"), hashlib.sha256).hexdigest()
        return f"{encoded}.{signature}"

    def verify(self, token: str) -> bool:
        try:
            encoded, provided_signature = token.split(".", maxsplit=1)
            expected_signature = hmac.new(
                self._secret,
                encoded.encode("ascii", errors="strict"),
                hashlib.sha256,
            ).hexdigest()
        except (UnicodeEncodeError, ValueError):
            return False
        if not hmac.compare_digest(provided_signature, expected_signature):
            return False
        try:
            padding = "=" * (-len(encoded) % 4)
            payload = base64.b64decode(
                f"{encoded}{padding}",
                altchars=b"-_",
                validate=True,
            ).decode("ascii")
            expires_at_text, nonce = payload.split(":", maxsplit=1)
            expires_at = int(expires_at_text)
        except (
            UnicodeEncodeError,
            UnicodeDecodeError,
            binascii.Error,
            ValueError,
        ):
            return False
        return bool(nonce) and int(time.time()) <= expires_at
