"""微信 iLink 网关：连接内置协议客户端与应用领域端口。"""

from __future__ import annotations

import asyncio
import logging
from contextlib import suppress
from enum import StrEnum
from typing import Literal, Never

from ...core.config import Settings
from ...domain.errors import (
    GatewayNotReadyError,
    GatewayRejectedError,
    GatewayTemporaryError,
    MissingContextError,
)
from ...domain.models import (
    GatewayPhase,
    GatewayStatus,
    OutboundPayload,
    WeChatDeliveryReceipt,
)
from ...domain.ports import InboundPublisher, WeChatStateStore
from ...domain.references import scoped_protocol_item_id
from .delivery import deliver_outbound_payload
from .ilink.auth import CredentialStore, QRAuthenticator
from .ilink.client import ILinkClient
from .ilink.errors import (
    ILinkApiError,
    ILinkMediaError,
    ILinkNetworkError,
    ILinkProtocolError,
)
from .ilink.models import Credentials, IncomingMessage, UpdateBatch
from .ilink.transport import ILinkTransport
from .inbound import InboundMessageProcessor
from .protocols import GatewayClient, TransportFactory
from .session import credential_fingerprint

logger = logging.getLogger(__name__)


class _ConnectionOutcome(StrEnum):
    INTERRUPTED = "interrupted"
    SESSION_EXPIRED = "session_expired"
    ERROR = "error"


class WeChatILinkGateway:
    """项目内置 iLink 客户端的领域适配器。"""

    def __init__(
        self,
        *,
        settings: Settings,
        store: WeChatStateStore,
        dispatcher: InboundPublisher,
        transport_factory: TransportFactory | None = None,
    ) -> None:
        self._settings = settings
        self._store = store
        self._dispatcher = dispatcher
        self._inbound = InboundMessageProcessor(
            store=store,
            dispatcher=dispatcher,
            media_download_timeout_seconds=settings.inbound_media_download_timeout_seconds,
        )
        self._transport_factory = transport_factory or ILinkTransport
        self._transport: ILinkTransport | None = None
        self._client: GatewayClient | None = None
        self._credentials: Credentials | None = None
        self._cursor = ""
        self._session_fingerprint: str | None = None
        self._stop_event = asyncio.Event()
        self._login_request_event = asyncio.Event()
        self._logout_request_event = asyncio.Event()
        self._connection_interrupt_event = asyncio.Event()
        self._logout_completed_event = asyncio.Event()
        self._logout_lock = asyncio.Lock()
        self._runner_active = False
        self._logout_error: Exception | None = None
        self._login_qr_url: str | None = None
        self._verification_future: asyncio.Future[str] | None = None
        self._status = GatewayStatus(
            phase=GatewayPhase.STOPPED,
            authenticated=False,
            listener_running=False,
        )

    @property
    def status(self) -> GatewayStatus:
        return self._status

    @property
    def session_fingerprint(self) -> str | None:
        return self._session_fingerprint

    @property
    def login_qr_url(self) -> str | None:
        """只供受 Basic Auth 保护的管理端即时渲染，不持久化二维码内容。"""

        return self._login_qr_url

    async def run(self) -> None:
        if self._runner_active:
            raise RuntimeError("WeChat gateway is already running")
        self._runner_active = True
        self._stop_event.clear()
        credential_store = CredentialStore(self._settings.credentials_path)
        try:
            credentials = await self._load_stored_credentials(credential_store)
            if credentials is None and self._status.phase is not GatewayPhase.ERROR:
                self._set_status(GatewayPhase.LOGIN_REQUIRED)

            while not self._stop_event.is_set():
                if self._logout_request_event.is_set():
                    await self._complete_logout(credential_store)
                    credentials = None
                    continue

                if credentials is not None:
                    self._connection_interrupt_event.clear()
                    self._set_status(GatewayPhase.LOGGING_IN)
                    outcome = await self._connection_cycle(
                        initial_credentials=credentials,
                        credential_store=credential_store,
                    )
                    credentials = None
                    await self._handle_connection_outcome(outcome, credential_store)
                    continue

                command = await self._wait_for_idle_command()
                if command == "stop":
                    break
                if command == "logout":
                    await self._complete_logout(credential_store)
                    continue

                self._login_request_event.clear()
                self._connection_interrupt_event.clear()
                self._set_status(GatewayPhase.LOGGING_IN)
                outcome = await self._connection_cycle(
                    initial_credentials=None,
                    credential_store=credential_store,
                )
                await self._handle_connection_outcome(outcome, credential_store)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("微信 iLink 网关生命周期发生异常")
            if not self._stop_event.is_set():
                self._set_status(
                    GatewayPhase.ERROR,
                    error_code="gateway_lifecycle_error",
                )
        finally:
            self._runner_active = False
            self._cancel_verification()
            self._clear_runtime_session()
            if self._logout_request_event.is_set():
                # 防止生命周期被外部取消时让正在等待的管理请求永久挂起。
                self._logout_error = RuntimeError("WeChat gateway stopped during logout")
                self._logout_completed_event.set()
            if self._stop_event.is_set():
                self._set_status(GatewayPhase.STOPPED)

    async def stop(self) -> None:
        self._stop_event.set()
        self._connection_interrupt_event.set()

    async def start_login(self) -> None:
        """让常驻网关开始一次新的扫码授权；重复点击不会创建并发流程。"""

        if self._stop_event.is_set():
            raise GatewayNotReadyError("WeChat gateway is stopping")
        if self._status.phase in {GatewayPhase.LOGGING_IN, GatewayPhase.LISTENING}:
            return
        self._login_request_event.set()

    async def submit_verification_code(self, code: str) -> None:
        normalized = code.strip()
        if not normalized or len(normalized) > 32 or not normalized.isdecimal():
            raise ValueError("The WeChat verification code must contain only digits")
        future = self._verification_future
        if future is None or future.done():
            raise GatewayNotReadyError("WeChat is not waiting for a verification code")
        future.set_result(normalized)

    async def logout(self) -> None:
        """停止当前授权流程或长轮询，删除授权文件并排空旧入站事件。"""

        async with self._logout_lock:
            self._logout_error = None
            self._logout_completed_event.clear()
            if not self._runner_active:
                self._cancel_verification()
                self._clear_runtime_session()
                await self._dispatcher.drain()
                await CredentialStore(self._settings.credentials_path).clear()
                self._set_status(GatewayPhase.LOGIN_REQUIRED)
                return

            self._logout_request_event.set()
            self._connection_interrupt_event.set()
            await self._logout_completed_event.wait()
            if self._logout_error is not None:
                raise RuntimeError(
                    "Failed to clear the WeChat authorization"
                ) from self._logout_error

    async def _load_stored_credentials(
        self,
        credential_store: CredentialStore,
    ) -> Credentials | None:
        try:
            return await credential_store.load()
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("读取本地微信授权文件失败")
            self._set_status(GatewayPhase.ERROR, error_code="credential_load_error")
            return None

    async def _connection_cycle(
        self,
        *,
        initial_credentials: Credentials | None,
        credential_store: CredentialStore,
    ) -> _ConnectionOutcome:
        transport = self._transport_factory()
        self._transport = transport
        try:
            credentials = initial_credentials
            if credentials is None:
                authenticator = self._build_authenticator(transport, credential_store)
                credentials = await self._authorize_or_interrupt(authenticator)
                if credentials is None:
                    return _ConnectionOutcome.INTERRUPTED
            if self._connection_interrupt_event.is_set():
                return _ConnectionOutcome.INTERRUPTED

            await self._install_session(credentials, transport=transport, restore=True)
            self._login_qr_url = None
            self._set_status(GatewayPhase.LISTENING)
            await self._notify_start()
            return await self._poll_loop()
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("微信 iLink 单次连接周期发生异常")
            error_code = (
                "login_error" if self._status.phase is GatewayPhase.LOGGING_IN else "listener_error"
            )
            self._set_status(GatewayPhase.ERROR, error_code=error_code)
            return _ConnectionOutcome.ERROR
        finally:
            try:
                await self._notify_stop()
            except Exception:
                logger.warning("发送微信停止通知时发生未预期异常", exc_info=True)
            try:
                if not self._logout_request_event.is_set():
                    await self._checkpoint_cursor()
            except Exception:
                logger.warning("保存微信长轮询游标失败", exc_info=True)
            try:
                await transport.close()
            except Exception:
                logger.warning("关闭微信 iLink 传输连接失败", exc_info=True)
            finally:
                self._transport = None
                self._cancel_verification()
                self._clear_runtime_session()

    async def _authorize_or_interrupt(
        self,
        authenticator: QRAuthenticator,
    ) -> Credentials | None:
        authorize_task = asyncio.create_task(
            authenticator.authorize(force=True),
            name="wechat-ilink-web-login",
        )
        interrupt_task = asyncio.create_task(
            self._connection_interrupt_event.wait(),
            name="wechat-ilink-login-interrupt",
        )
        try:
            done, _pending = await asyncio.wait(
                {authorize_task, interrupt_task},
                return_when=asyncio.FIRST_COMPLETED,
            )
            if interrupt_task in done:
                authorize_task.cancel()
                with suppress(asyncio.CancelledError):
                    await authorize_task
                return None
            interrupt_task.cancel()
            with suppress(asyncio.CancelledError):
                await interrupt_task
            return await authorize_task
        finally:
            for task in (authorize_task, interrupt_task):
                if not task.done():
                    task.cancel()
            await asyncio.gather(authorize_task, interrupt_task, return_exceptions=True)

    async def _handle_connection_outcome(
        self,
        outcome: _ConnectionOutcome,
        credential_store: CredentialStore,
    ) -> None:
        if outcome is not _ConnectionOutcome.SESSION_EXPIRED:
            return
        try:
            await credential_store.clear()
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("清除已过期微信授权失败")
            self._set_status(GatewayPhase.ERROR, error_code="credential_clear_error")
            return
        self._set_status(GatewayPhase.LOGIN_REQUIRED)

    async def _wait_for_idle_command(self) -> Literal["login", "logout", "stop"]:
        if self._stop_event.is_set():
            return "stop"
        if self._logout_request_event.is_set():
            return "logout"
        if self._login_request_event.is_set():
            return "login"

        tasks = {
            "login": asyncio.create_task(
                self._login_request_event.wait(),
                name="wechat-login-request-wait",
            ),
            "logout": asyncio.create_task(
                self._logout_request_event.wait(),
                name="wechat-logout-request-wait",
            ),
            "stop": asyncio.create_task(
                self._stop_event.wait(),
                name="wechat-stop-request-wait",
            ),
        }
        try:
            done, _pending = await asyncio.wait(
                set(tasks.values()),
                return_when=asyncio.FIRST_COMPLETED,
            )
            if tasks["stop"] in done:
                return "stop"
            if tasks["logout"] in done:
                return "logout"
            if tasks["login"] in done:
                return "login"
            raise RuntimeError("WeChat gateway control wait returned without a command")
        finally:
            for task in tasks.values():
                if not task.done():
                    task.cancel()
            await asyncio.gather(*tasks.values(), return_exceptions=True)

    async def _complete_logout(self, credential_store: CredentialStore) -> None:
        try:
            self._cancel_verification()
            self._clear_runtime_session()
            # 生产者已停止；等旧事件完成落库/转发后，应用服务才能安全清库。
            await self._dispatcher.drain()
            await credential_store.clear()
            self._set_status(GatewayPhase.LOGIN_REQUIRED)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self._logout_error = exc
            logger.exception("停止并清除微信授权失败")
            self._set_status(GatewayPhase.ERROR, error_code="logout_error")
        finally:
            self._login_request_event.clear()
            self._logout_request_event.clear()
            if not self._stop_event.is_set():
                self._connection_interrupt_event.clear()
            self._logout_completed_event.set()

    async def send_message(
        self,
        *,
        message_id: str,
        user_id: str,
        payload: OutboundPayload,
        context_token: str,
    ) -> WeChatDeliveryReceipt:
        client = self._client
        credentials = self._credentials
        if (
            client is None
            or credentials is None
            or self._status.phase is not GatewayPhase.LISTENING
            or not self._session_fingerprint
        ):
            raise GatewayNotReadyError("WeChat is not connected")
        if not context_token:
            raise MissingContextError("The owner must send the bot a new message first")
        if credential_fingerprint(credentials) != self._session_fingerprint:
            raise MissingContextError(
                "The WeChat session changed; send the bot a new message first"
            )

        try:
            server_message_id = await deliver_outbound_payload(
                client,
                message_id=message_id,
                user_id=user_id,
                context_token=context_token,
                payload=payload,
            )
            protocol_item_id = scoped_protocol_item_id(
                credentials.account_id,
                server_message_id,
            )
            if server_message_id is not None and protocol_item_id is None:
                logger.warning("微信发送成功，但服务端消息 ID 无法规范化")
            return WeChatDeliveryReceipt(
                wechat_message_id=server_message_id,
                protocol_item_id=protocol_item_id,
            )
        except ILinkApiError as exc:
            self._raise_gateway_api_error(exc)
        except ILinkMediaError as exc:
            if exc.temporary:
                raise GatewayTemporaryError(
                    "WeChat media transport is temporarily unavailable"
                ) from exc
            raise GatewayRejectedError("WeChat rejected the media content") from exc
        except (ILinkNetworkError, ILinkProtocolError) as exc:
            raise GatewayTemporaryError("A temporary WeChat transport error occurred") from exc

    async def _poll_loop(self) -> _ConnectionOutcome:
        retry_delay = 1.0
        while not self._connection_interrupt_event.is_set():
            client = self._require_client()
            try:
                batch = await self._poll_or_interrupt(client)
                if batch is None:
                    return _ConnectionOutcome.INTERRUPTED
                # 先完整处理消息再提交新 cursor；处理失败时重启仍可依靠事件 ID 去重重放。
                for message in batch.messages:
                    await self._handle_incoming(message, client=client)
                if batch.cursor != self._cursor:
                    self._cursor = batch.cursor
                    await self._checkpoint_cursor()
                retry_delay = 1.0
            except ILinkApiError as exc:
                if exc.is_session_expired:
                    return _ConnectionOutcome.SESSION_EXPIRED
                logger.warning(
                    "微信 iLink 长轮询被上游拒绝，稍后重试",
                    extra={
                        "http_status": exc.http_status,
                        "errcode": exc.errcode,
                        "retry_delay_seconds": retry_delay,
                    },
                )
                await self._wait_before_retry(retry_delay)
                retry_delay = min(retry_delay * 2, 10.0)
            except (ILinkNetworkError, ILinkProtocolError):
                logger.warning(
                    "微信 iLink 长轮询暂时失败，稍后重试",
                    extra={"retry_delay_seconds": retry_delay},
                )
                await self._wait_before_retry(retry_delay)
                retry_delay = min(retry_delay * 2, 10.0)
        return _ConnectionOutcome.INTERRUPTED

    async def _poll_or_interrupt(self, client: GatewayClient) -> UpdateBatch | None:
        poll_task = asyncio.create_task(
            client.get_updates(self._cursor),
            name="wechat-ilink-long-poll",
        )
        interrupt_task = asyncio.create_task(
            self._connection_interrupt_event.wait(),
            name="wechat-ilink-interrupt-wait",
        )
        try:
            done, _pending = await asyncio.wait(
                {poll_task, interrupt_task},
                return_when=asyncio.FIRST_COMPLETED,
            )
            if interrupt_task in done:
                poll_task.cancel()
                with suppress(asyncio.CancelledError):
                    await poll_task
                return None
            interrupt_task.cancel()
            with suppress(asyncio.CancelledError):
                await interrupt_task
            return await poll_task
        finally:
            for task in (poll_task, interrupt_task):
                if not task.done():
                    task.cancel()
            await asyncio.gather(
                poll_task,
                interrupt_task,
                return_exceptions=True,
            )

    async def _wait_before_retry(self, delay: float) -> None:
        try:
            await asyncio.wait_for(self._connection_interrupt_event.wait(), timeout=delay)
        except TimeoutError:
            pass

    async def _handle_incoming(
        self,
        message: IncomingMessage,
        *,
        client: GatewayClient | None = None,
    ) -> None:
        active_client = client or self._require_client()
        credentials = self._credentials
        if credentials is None:
            return
        current_fingerprint = credential_fingerprint(credentials)
        if current_fingerprint != self._session_fingerprint:
            return
        await self._inbound.process(
            message,
            client=active_client,
            credentials=credentials,
            session_fingerprint=current_fingerprint,
        )

    async def _install_session(
        self,
        credentials: Credentials,
        *,
        transport: ILinkTransport,
        restore: bool,
    ) -> None:
        fingerprint = credential_fingerprint(credentials)
        changed = await self._store.activate_session(fingerprint)
        self._session_fingerprint = fingerprint
        self._credentials = credentials
        self._cursor = ""
        await self._store.reconcile_owner_binding(
            user_id=credentials.user_id,
            credential_fingerprint=fingerprint,
        )
        if restore and not changed:
            cursor = await self._store.get_state(f"cursor:{fingerprint}")
            if cursor:
                self._cursor = cursor
        self._client = ILinkClient(
            credentials=credentials,
            transport=transport,
            media_max_bytes=self._settings.media_max_bytes,
        )

    async def _checkpoint_cursor(self) -> None:
        if self._session_fingerprint and self._cursor:
            await self._store.set_state(
                f"cursor:{self._session_fingerprint}",
                self._cursor,
            )

    def _build_authenticator(
        self,
        transport: ILinkTransport,
        credential_store: CredentialStore,
    ) -> QRAuthenticator:
        return QRAuthenticator(
            transport=transport,
            credentials=credential_store,
            on_qr_url=self._on_qr_url,
            on_scanned=self._on_scanned,
            on_expired=self._on_expired,
            on_verify_code=self._request_verification_code,
        )

    async def _notify_start(self) -> None:
        client = self._client
        if client is None:
            return
        try:
            await client.notify_start()
        except (ILinkApiError, ILinkNetworkError, ILinkProtocolError):
            logger.warning("发送微信启动通知失败，继续进入监听")

    async def _notify_stop(self) -> None:
        client = self._client
        if client is None:
            return
        try:
            async with asyncio.timeout(5):
                await client.notify_stop()
        except (ILinkApiError, ILinkNetworkError, ILinkProtocolError, TimeoutError):
            logger.warning("关闭连接时发送微信停止通知失败")

    def _raise_gateway_api_error(self, error: ILinkApiError) -> Never:
        if error.errcode == -2:
            raise GatewayRejectedError(
                "The context may be stale or WeChat may be rate limiting messages"
            ) from error
        if error.is_session_expired:
            raise GatewayNotReadyError("The WeChat session expired") from error
        if error.http_status == 429 or error.http_status >= 500:
            raise GatewayTemporaryError("WeChat temporarily rejected the request") from error
        raise GatewayRejectedError("WeChat rejected the message") from error

    def _on_qr_url(self, url: str) -> None:
        self._login_qr_url = url
        self._set_status(GatewayPhase.LOGGING_IN, qr_ready=True)

    def _on_scanned(self) -> None:
        self._login_qr_url = None
        self._set_status(GatewayPhase.LOGGING_IN)
        logger.info("微信登录二维码已扫描，等待账号确认")

    def _on_expired(self) -> None:
        self._login_qr_url = None
        self._set_status(GatewayPhase.LOGGING_IN)
        logger.warning("微信登录二维码已过期，准备申请新二维码")

    async def _request_verification_code(self, retry: bool) -> str:
        self._login_qr_url = None
        loop = asyncio.get_running_loop()
        future: asyncio.Future[str] = loop.create_future()
        self._verification_future = future
        self._set_status(
            GatewayPhase.LOGGING_IN,
            verification_required=True,
            error_code="verification_code_retry" if retry else None,
        )
        try:
            return await future
        finally:
            if self._verification_future is future:
                self._verification_future = None
            if (
                not self._connection_interrupt_event.is_set()
                and self._status.phase is GatewayPhase.LOGGING_IN
            ):
                self._set_status(GatewayPhase.LOGGING_IN)

    def _cancel_verification(self) -> None:
        future = self._verification_future
        self._verification_future = None
        if future is not None and not future.done():
            future.cancel()

    def _clear_runtime_session(self) -> None:
        self._client = None
        self._credentials = None
        self._cursor = ""
        self._session_fingerprint = None
        self._login_qr_url = None

    def _set_status(
        self,
        phase: GatewayPhase,
        *,
        authenticated: bool | None = None,
        listener_running: bool | None = None,
        qr_ready: bool = False,
        verification_required: bool = False,
        error_code: str | None = None,
    ) -> None:
        previous = self._status
        current = GatewayStatus(
            phase=phase,
            authenticated=(
                authenticated if authenticated is not None else phase is GatewayPhase.LISTENING
            ),
            listener_running=(
                listener_running
                if listener_running is not None
                else phase is GatewayPhase.LISTENING
            ),
            qr_ready=qr_ready,
            verification_required=verification_required,
            error_code=error_code,
        )
        self._status = current
        if current != previous:
            logger.info(
                "微信网关状态已变化",
                extra={
                    "from_phase": previous.phase.value,
                    "to_phase": current.phase.value,
                    "error_code": current.error_code,
                },
            )

    def _require_client(self) -> GatewayClient:
        if self._client is None:
            raise GatewayNotReadyError("WeChat is not connected")
        return self._client
