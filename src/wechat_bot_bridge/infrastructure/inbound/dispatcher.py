"""非阻塞的入站事件扇出。"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
import time
from contextlib import suppress

import httpx

from ...domain.models import InboundEvent
from ...domain.ports import InboundSink

logger = logging.getLogger(__name__)


class LoggingSink:
    """只记录不敏感的事件元数据。"""

    async def deliver(self, event: InboundEvent) -> None:
        logger.info(
            "微信入站事件已完成统一分发",
            extra={
                "event_id": event.event_id,
                "message_type": event.message.type,
            },
        )

    async def close(self) -> None:
        return None


class WebhookSink:
    """使用 HMAC 签名和有上限重试投递脱敏事件。"""

    def __init__(
        self,
        *,
        url: str,
        secret: str,
        timeout_seconds: float,
        max_attempts: int,
    ) -> None:
        self._url = url
        self._secret = secret.encode("utf-8")
        self._max_attempts = max_attempts
        self._client = httpx.AsyncClient(timeout=timeout_seconds, follow_redirects=False)

    async def deliver(self, event: InboundEvent) -> None:
        payload = json.dumps(
            event.model_dump(mode="json"),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        timestamp = str(int(time.time()))
        signature = hmac.new(
            self._secret,
            timestamp.encode("ascii") + b"." + payload,
            hashlib.sha256,
        ).hexdigest()
        headers = {
            "Content-Type": "application/json",
            "X-WeChat-Bridge-Event": event.event_id,
            "X-WeChat-Bridge-Timestamp": timestamp,
            "X-WeChat-Bridge-Signature": f"sha256={signature}",
        }

        for attempt in range(1, self._max_attempts + 1):
            try:
                response = await self._client.post(self._url, content=payload, headers=headers)
                response.raise_for_status()
                return
            except (httpx.TimeoutException, httpx.NetworkError, httpx.HTTPStatusError) as exc:
                delay = min(2 ** (attempt - 1), 4)
                http_status = (
                    exc.response.status_code if isinstance(exc, httpx.HTTPStatusError) else None
                )
                logger.warning(
                    "入站 Webhook 投递失败",
                    extra={
                        "event_id": event.event_id,
                        "attempt": attempt,
                        "max_attempts": self._max_attempts,
                        "http_status": http_status,
                        "retry_delay_seconds": (delay if attempt < self._max_attempts else None),
                    },
                )
                if attempt < self._max_attempts:
                    await asyncio.sleep(delay)
        # 失败原因已按安全字段记录；异常链可能包含 Webhook URL，因此不向上透传。
        raise RuntimeError("Inbound webhook delivery exhausted its retry budget") from None

    async def close(self) -> None:
        await self._client.aclose()


class InboundDispatcher:
    """用有界队列隔离慢速出口，避免阻塞 iLink 长轮询处理。"""

    def __init__(self, sinks: list[InboundSink], *, queue_size: int) -> None:
        self._sinks = sinks
        self._queue: asyncio.Queue[InboundEvent] = asyncio.Queue(maxsize=queue_size)
        self._task: asyncio.Task[None] | None = None
        self._stopping = False

    @property
    def running(self) -> bool:
        return self._task is not None and not self._task.done()

    async def start(self) -> None:
        if self.running:
            return
        self._stopping = False
        self._task = asyncio.create_task(self._run(), name="inbound-dispatcher")
        logger.info("入站分发器已启动")

    async def publish(self, event: InboundEvent) -> None:
        """快速入队；仅通过有界等待施加背压，不在此处执行网络 I/O。"""

        await self._queue.put(event)

    async def drain(self) -> None:
        """等待已经接收的入站事件完成全部 Sink 投递。"""

        if not self.running and not self._queue.empty():
            raise RuntimeError("Inbound dispatcher stopped before its queue was drained")
        await self._queue.join()

    async def stop(self) -> None:
        self._stopping = True
        task = self._task
        if task is not None:
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task
        self._task = None
        for sink in self._sinks:
            try:
                await sink.close()
            except Exception:
                logger.exception(
                    "关闭入站 Sink 失败",
                    extra={"sink": type(sink).__name__},
                )
        logger.info("入站分发器已停止")

    async def _run(self) -> None:
        while not self._stopping:
            event = await self._queue.get()
            try:
                for sink in self._sinks:
                    try:
                        await sink.deliver(event)
                    except asyncio.CancelledError:
                        raise
                    except Exception:
                        logger.exception(
                            "入站 Sink 投递失败",
                            extra={
                                "event_id": event.event_id,
                                "sink": type(sink).__name__,
                            },
                        )
            finally:
                self._queue.task_done()
