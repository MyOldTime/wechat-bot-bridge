"""iLink HTTP 头、错误映射、超时与 CDN 边界测试。"""

from __future__ import annotations

import json

import httpx
import pytest

from wechat_bot_bridge.infrastructure.wechat.ilink import transport as transport_module
from wechat_bot_bridge.infrastructure.wechat.ilink.errors import (
    ILinkApiError,
    ILinkMediaError,
)
from wechat_bot_bridge.infrastructure.wechat.ilink.models import Credentials
from wechat_bot_bridge.infrastructure.wechat.ilink.transport import ILinkTransport


def _credentials() -> Credentials:
    return Credentials(
        token="fake-token",
        base_url="https://ilink.example.invalid",
        account_id="account-1",
        user_id="user-1",
    )


async def test_get_updates_builds_required_headers_and_body() -> None:
    captured: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return httpx.Response(
            200,
            json={
                "ret": 0,
                "msgs": [],
                "get_updates_buf": "cursor-2",
            },
        )

    http_client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    transport = ILinkTransport(http_client=http_client)
    try:
        response = await transport.get_updates(_credentials(), "cursor-1")
    finally:
        await http_client.aclose()

    assert response["get_updates_buf"] == "cursor-2"
    request = captured[0]
    assert request.url.path == "/ilink/bot/getupdates"
    assert request.headers["authorization"] == "Bearer fake-token"
    assert request.headers["authorizationtype"] == "ilink_bot_token"
    assert request.headers["ilink-app-id"] == "bot"
    assert request.headers["x-wechat-uin"]
    body = json.loads(request.content)
    assert body["get_updates_buf"] == "cursor-1"
    assert body["base_info"]["bot_agent"].startswith("wechat-bot-bridge/")


async def test_api_error_exposes_codes_but_not_upstream_payload() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            429,
            json={
                "ret": -2,
                "errmsg": "must-not-leak",
                "context_token": "private-context",
            },
        )

    http_client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    transport = ILinkTransport(http_client=http_client)
    try:
        with pytest.raises(ILinkApiError) as caught:
            await transport.send_message(_credentials(), {"item_list": []})
    finally:
        await http_client.aclose()

    assert caught.value.http_status == 429
    assert caught.value.errcode == -2
    assert "must-not-leak" not in str(caught.value)
    assert "private-context" not in str(caught.value)


async def test_long_poll_read_timeout_is_a_normal_empty_batch() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("normal long-poll timeout", request=request)

    http_client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    transport = ILinkTransport(http_client=http_client)
    try:
        response = await transport.get_updates(_credentials(), "cursor-1")
    finally:
        await http_client.aclose()

    assert response == {
        "ret": 0,
        "msgs": [],
        "get_updates_buf": "cursor-1",
    }


async def test_cdn_download_enforces_streaming_size_limit() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={"Content-Length": "100"},
            content=b"x" * 100,
        )

    http_client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    transport = ILinkTransport(http_client=http_client)
    try:
        with pytest.raises(ILinkMediaError, match="size limit"):
            await transport.download_cdn(
                url="https://cdn.example.invalid/download",
                max_bytes=32,
            )
    finally:
        await http_client.aclose()


async def test_cdn_upload_retries_server_errors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    attempts = 0

    async def no_sleep(_seconds: float) -> None:
        return None

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        if attempts < 3:
            return httpx.Response(503)
        return httpx.Response(
            200,
            headers={"x-encrypted-param": "download-reference"},
        )

    monkeypatch.setattr(transport_module.asyncio, "sleep", no_sleep)
    http_client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    transport = ILinkTransport(http_client=http_client)
    try:
        result = await transport.upload_cdn(
            url="https://cdn.example.invalid/upload",
            ciphertext=b"encrypted",
        )
    finally:
        await http_client.aclose()

    assert result == "download-reference"
    assert attempts == 3
