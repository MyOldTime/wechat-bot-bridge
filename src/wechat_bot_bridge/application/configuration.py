"""MCP 凭据和单向消息转发配置。"""

from __future__ import annotations

import asyncio
import secrets
from datetime import UTC, datetime

from pydantic import ValidationError

from ..domain.models import (
    ForwardingScheme,
    OutboundForwardingConfig,
    PublicApiTokenSnapshot,
)
from ..domain.ports import ServiceConfigurationStore

_PUBLIC_API_TOKEN_KEY = "public_api_token_v1"
_OUTBOUND_FORWARDING_KEY = "outbound_forwarding_v1"


def _now() -> datetime:
    return datetime.now(UTC)


def _new_public_api_token() -> str:
    return f"wbb_live_{secrets.token_urlsafe(32)}"


def _new_forwarding_secret() -> str:
    return f"wbb_hook_{secrets.token_urlsafe(32)}"


class PublicApiCredentialService:
    """生成、持久化并恒定时间校验 MCP Token。"""

    def __init__(self, store: ServiceConfigurationStore) -> None:
        self._store = store
        self._lock = asyncio.Lock()
        self._snapshot: PublicApiTokenSnapshot | None = None

    async def initialize(self) -> PublicApiTokenSnapshot:
        async with self._lock:
            if self._snapshot is not None:
                return self._snapshot
            serialized = await self._store.get_service_setting(_PUBLIC_API_TOKEN_KEY)
            if serialized is None:
                snapshot = PublicApiTokenSnapshot(
                    token=_new_public_api_token(),
                    updated_at=_now(),
                )
                await self._persist(snapshot)
            else:
                try:
                    snapshot = PublicApiTokenSnapshot.model_validate_json(serialized)
                except ValidationError as exc:
                    raise RuntimeError("Stored public API token is invalid") from exc
            self._snapshot = snapshot
            return snapshot

    async def current(self) -> PublicApiTokenSnapshot:
        snapshot = self._snapshot
        if snapshot is None:
            return await self.initialize()
        return snapshot

    async def matches(self, provided: str) -> bool:
        expected = (await self.current()).token
        return secrets.compare_digest(provided if provided.isascii() else "", expected)

    async def rotate(self) -> PublicApiTokenSnapshot:
        async with self._lock:
            snapshot = PublicApiTokenSnapshot(
                token=_new_public_api_token(),
                updated_at=_now(),
            )
            await self._persist(snapshot)
            self._snapshot = snapshot
            return snapshot

    async def _persist(self, snapshot: PublicApiTokenSnapshot) -> None:
        await self._store.set_service_setting(
            _PUBLIC_API_TOKEN_KEY,
            snapshot.model_dump_json(),
        )


class OutboundForwardingConfigurationService:
    """维护立即生效且不依赖进程重启的转发配置快照。"""

    def __init__(self, store: ServiceConfigurationStore) -> None:
        self._store = store
        self._lock = asyncio.Lock()
        self._snapshot: OutboundForwardingConfig | None = None

    async def initialize(self) -> OutboundForwardingConfig:
        async with self._lock:
            if self._snapshot is not None:
                return self._snapshot
            serialized = await self._store.get_service_setting(_OUTBOUND_FORWARDING_KEY)
            if serialized is None:
                snapshot = OutboundForwardingConfig(
                    secret=_new_forwarding_secret(),
                    updated_at=_now(),
                )
                await self._persist(snapshot)
            else:
                try:
                    snapshot = OutboundForwardingConfig.model_validate_json(serialized)
                except ValidationError as exc:
                    raise RuntimeError(
                        "Stored outbound forwarding configuration is invalid"
                    ) from exc
            self._snapshot = snapshot
            return snapshot

    async def current(self) -> OutboundForwardingConfig:
        snapshot = self._snapshot
        if snapshot is None:
            return await self.initialize()
        return snapshot

    async def update(
        self,
        *,
        enabled: bool,
        scheme: ForwardingScheme,
        host: str,
        port: int,
        path: str,
        secret: str | None,
        timeout_seconds: float,
        max_attempts: int,
    ) -> OutboundForwardingConfig:
        async with self._lock:
            previous = self._snapshot
            if previous is None:
                serialized = await self._store.get_service_setting(_OUTBOUND_FORWARDING_KEY)
                previous = (
                    OutboundForwardingConfig.model_validate_json(serialized)
                    if serialized is not None
                    else None
                )
            snapshot = OutboundForwardingConfig(
                enabled=enabled,
                scheme=scheme,
                host=host,
                port=port,
                path=path,
                secret=secret or (previous.secret if previous else _new_forwarding_secret()),
                timeout_seconds=timeout_seconds,
                max_attempts=max_attempts,
                updated_at=_now(),
            )
            await self._persist(snapshot)
            self._snapshot = snapshot
            return snapshot

    async def rotate_secret(self) -> OutboundForwardingConfig:
        async with self._lock:
            current = self._snapshot
            if current is None:
                serialized = await self._store.get_service_setting(_OUTBOUND_FORWARDING_KEY)
                current = (
                    OutboundForwardingConfig.model_validate_json(serialized)
                    if serialized is not None
                    else OutboundForwardingConfig(
                        secret=_new_forwarding_secret(),
                        updated_at=_now(),
                    )
                )
            snapshot = current.model_copy(
                update={
                    "secret": _new_forwarding_secret(),
                    "updated_at": _now(),
                }
            )
            await self._persist(snapshot)
            self._snapshot = snapshot
            return snapshot

    async def _persist(self, snapshot: OutboundForwardingConfig) -> None:
        await self._store.set_service_setting(
            _OUTBOUND_FORWARDING_KEY,
            snapshot.model_dump_json(),
        )
