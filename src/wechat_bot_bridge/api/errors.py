"""发送用例到稳定 HTTP 错误的共享映射。"""

from __future__ import annotations

from collections.abc import Awaitable, Callable

from fastapi import HTTPException

from ..application.messaging import NotificationService
from ..domain.errors import (
    GatewayNotReadyError,
    GatewayRejectedError,
    GatewayTemporaryError,
    MissingContextError,
    OwnerNotBoundError,
)
from ..domain.models import MessageOrigin, OutboundMessage, OutboundPayload


def http_error(status_code: int, code: str, message: str) -> HTTPException:
    return HTTPException(
        status_code=status_code,
        detail={"code": code, "message": message},
    )


async def send_with_http_errors(
    *,
    service: NotificationService,
    payload: OutboundPayload,
    origin: MessageOrigin,
) -> OutboundMessage:
    return await outbound_with_http_errors(
        lambda: service.send(
            payload=payload,
            origin=origin,
        )
    )


async def outbound_with_http_errors(
    operation: Callable[[], Awaitable[OutboundMessage]],
) -> OutboundMessage:
    """把任意同步出站用例映射为一致的 HTTP 错误。"""

    try:
        return await operation()
    except OwnerNotBoundError as exc:
        raise http_error(409, exc.code, str(exc)) from exc
    except MissingContextError as exc:
        raise http_error(409, exc.code, str(exc)) from exc
    except GatewayNotReadyError as exc:
        raise http_error(503, exc.code, str(exc)) from exc
    except GatewayTemporaryError as exc:
        raise http_error(502, exc.code, str(exc)) from exc
    except GatewayRejectedError as exc:
        raise http_error(502, exc.code, str(exc)) from exc
    except ValueError as exc:
        raise http_error(422, "invalid_message", str(exc)) from exc
