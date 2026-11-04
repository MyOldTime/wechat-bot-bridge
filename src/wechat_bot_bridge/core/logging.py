"""容器友好的安全日志配置。

业务模块通过 ``extra`` 传递的上下文默认不会出现在标准日志格式中。本模块
只输出经过白名单审核的字段，使日志既便于检索，又不会意外带出聊天正文、
账号标识、上下文令牌或授权材料。
"""

from __future__ import annotations

import json
import logging
from collections.abc import Mapping
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Final

_CONTEXT_FIELDS: Final[tuple[str, ...]] = (
    "event_id_hash",
    "message_id",
    "message_type",
    "origin",
    "delivery_status",
    "error_code",
    "from_phase",
    "to_phase",
    "http_status",
    "errcode",
    "attempt",
    "max_attempts",
    "retry_delay_seconds",
    "elapsed_ms",
    "target_scheme",
    "target_host",
    "target_port",
    "sink",
)
_LOG_FORMAT: Final[str] = "%(asctime)s %(levelname)s %(name)s %(message)s"


class SafeContextFormatter(logging.Formatter):
    """在普通日志后附加白名单 JSON 上下文。"""

    def format(self, record: logging.LogRecord) -> str:
        rendered = super().format(record)
        context = _safe_context(record)
        if not context:
            return rendered
        serialized = json.dumps(
            context,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )
        return f"{rendered} context={serialized}"


def _safe_log_source(record: logging.LogRecord) -> bool:
    if record.name.startswith("wechat_bot_bridge."):
        return True
    if record.name == "uvicorn.access" and isinstance(record.args, tuple):
        return len(record.args) == 5 and "?" not in str(record.args[2])
    return record.name == "uvicorn.error" and record.levelno >= logging.INFO


def configure_logging(level: str, *, log_file: Path | None = None) -> None:
    """为 CLI 进程安装统一日志处理器。

    Uvicorn 使用同一个根处理器，应用日志和访问日志因而具有一致的时间、
    级别和模块名。``trace`` 在标准库中按 ``DEBUG`` 处理。
    """

    normalized = level.upper()
    numeric_level = (
        logging.DEBUG
        if normalized == "TRACE"
        else logging.getLevelNamesMapping().get(
            normalized,
            logging.INFO,
        )
    )
    handler: logging.Handler = (
        RotatingFileHandler(log_file, maxBytes=1024 * 1024, backupCount=2, encoding="utf-8")
        if log_file is not None
        else logging.StreamHandler()
    )
    handler.setFormatter(SafeContextFormatter(_LOG_FORMAT))
    # SDK 也用根 logger 输出校验输入，SQLite 调试日志包含绑定参数。
    handler.addFilter(_safe_log_source)

    root_logger = logging.getLogger()
    root_logger.handlers.clear()
    root_logger.addHandler(handler)
    root_logger.setLevel(numeric_level)

    # HTTP 客户端逐请求调试信息噪声较大；协议错误由本项目在更高层统一记录。
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    # MCP SDK 的调试消息和 Uvicorn 的 ASGI 跟踪可包含完整请求与认证头。
    logging.getLogger("mcp").setLevel(logging.CRITICAL + 1)
    logging.getLogger("aiosqlite").setLevel(logging.WARNING)
    logging.getLogger("uvicorn.asgi").disabled = True
    logging.captureWarnings(True)


def _safe_context(record: logging.LogRecord) -> Mapping[str, object]:
    context: dict[str, object] = {}
    for field in _CONTEXT_FIELDS:
        value = getattr(record, field, None)
        if value is None:
            continue
        if isinstance(value, (str, int, float, bool)):
            context[field] = value
        else:
            context[field] = str(value)
    return context
