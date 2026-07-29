"""FastAPI 应用工厂与受管理的微信监听生命周期。"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager, suppress
from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from ..api.external import build_external_router
from ..api.health import build_health_router
from ..application.account import AccountLifecycleService
from ..application.messaging import NotificationService
from ..application.typing import TypingLeaseService
from ..core.config import Settings
from ..domain.ports import InboundSink, WeChatGateway
from ..infrastructure.inbound.chat_history import ChatHistorySink
from ..infrastructure.inbound.dispatcher import (
    InboundDispatcher,
    LoggingSink,
    WebhookSink,
)
from ..infrastructure.persistence.sqlite import SQLiteStore
from ..infrastructure.realtime.conversation import ConversationHub
from ..infrastructure.wechat.gateway import WeChatILinkGateway
from ..web.router import build_web_router
from ..web.security import AdminTokenSigner

logger = logging.getLogger(__name__)

GatewayFactory = Callable[
    [Settings, SQLiteStore, InboundDispatcher],
    WeChatGateway,
]


def _default_gateway_factory(
    settings: Settings,
    store: SQLiteStore,
    dispatcher: InboundDispatcher,
) -> WeChatGateway:
    return WeChatILinkGateway(settings=settings, store=store, dispatcher=dispatcher)


def create_app(
    settings: Settings | None = None,
    *,
    gateway_factory: GatewayFactory | None = None,
) -> FastAPI:
    resolved_settings = settings or Settings()  # type: ignore[call-arg]
    factory = gateway_factory or _default_gateway_factory
    conversation_hub = ConversationHub()
    admin_token_signer = AdminTokenSigner(
        ttl_seconds=resolved_settings.web_token_ttl_seconds,
    )

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        logger.info("正在启动微信桥接服务")
        resolved_settings.prepare_directories()
        store = SQLiteStore(resolved_settings.database_path)
        await store.open()

        # 聊天记录必须先落库并广播；日志和可选 Webhook 属于后续独立出口。
        sinks: list[InboundSink] = [
            ChatHistorySink(store=store, publisher=conversation_hub),
            LoggingSink(),
        ]
        if resolved_settings.inbound_webhook_url is not None:
            secret = resolved_settings.inbound_webhook_secret
            if secret is None:  # 正常情况下已由 Settings 校验拦截
                raise RuntimeError("Webhook secret is missing")
            sinks.append(
                WebhookSink(
                    url=str(resolved_settings.inbound_webhook_url),
                    secret=secret.get_secret_value(),
                    timeout_seconds=resolved_settings.webhook_timeout_seconds,
                    max_attempts=resolved_settings.webhook_max_attempts,
                )
            )
        dispatcher = InboundDispatcher(
            sinks,
            queue_size=resolved_settings.inbound_queue_size,
        )
        await dispatcher.start()

        gateway = factory(resolved_settings, store, dispatcher)
        account_operation_lock = asyncio.Lock()
        service = NotificationService(
            store=store,
            gateway=gateway,
            publisher=conversation_hub,
            message_max_chars=resolved_settings.message_max_chars,
            media_max_bytes=resolved_settings.media_max_bytes,
            operation_lock=account_operation_lock,
        )
        account_lifecycle = AccountLifecycleService(
            store=store,
            gateway=gateway,
            publisher=conversation_hub,
            operation_lock=account_operation_lock,
        )
        typing_lease = TypingLeaseService(
            notifications=service,
            timeout_seconds=resolved_settings.web_typing_timeout_seconds,
        )

        app.state.store = store
        app.state.dispatcher = dispatcher
        app.state.gateway = gateway
        app.state.notification_service = service
        app.state.account_lifecycle = account_lifecycle
        app.state.typing_lease = typing_lease

        gateway_task = asyncio.create_task(gateway.run(), name="wechat-listener")
        app.state.gateway_task = gateway_task
        logger.info("微信桥接服务启动完成")
        try:
            yield
        finally:
            logger.info("正在停止微信桥接服务")
            await typing_lease.close()
            await gateway.stop()
            try:
                async with asyncio.timeout(
                    resolved_settings.inbound_media_download_timeout_seconds + 10
                ):
                    await gateway_task
            except TimeoutError:
                gateway_task.cancel()
                with suppress(asyncio.CancelledError):
                    await gateway_task
            await dispatcher.stop()
            await store.close()
            logger.info("微信桥接服务已安全停止")

    app = FastAPI(
        title="wechat-bot-bridge",
        version="0.1.0",
        description="Synchronous HTTP bridge for WeChat iLink Bot notifications",
        lifespan=lifespan,
    )
    app.state.settings = resolved_settings
    app.state.conversation_hub = conversation_hub
    app.state.admin_token_signer = admin_token_signer

    web_root = Path(__file__).resolve().parents[1] / "web"
    app.mount(
        "/admin/static",
        StaticFiles(directory=web_root / "static"),
        name="admin-static",
    )
    app.include_router(build_health_router())
    app.include_router(build_external_router())
    app.include_router(build_web_router())
    return app
