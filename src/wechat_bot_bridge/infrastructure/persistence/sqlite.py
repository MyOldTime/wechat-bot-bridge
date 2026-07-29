"""使用 SQLite 持久化 owner 会话、入站去重状态和发送审计。"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import aiosqlite

from ...domain.errors import QuotedMessageNotFoundError
from ...domain.models import (
    OWNER_ALIAS,
    ConversationDirection,
    ConversationMedia,
    ConversationMessage,
    ConversationMessagePage,
    ConversationStatus,
    InboundEvent,
    MessageOrigin,
    OutboundMessage,
    OutboundMessageType,
    OutboundPayload,
    OutboundStatus,
    OwnerBinding,
)
from .cursor import decode_conversation_cursor, encode_conversation_cursor
from .records import (
    conversation_from_row,
    outbound_chat_fields,
    outbound_from_row,
    owner_from_row,
)
from .schema import BUSINESS_TABLES, initialize_schema
from .transactions import immediate_transaction

_CONVERSATION_SELECT_SQL = """
    SELECT current_message.*,
           quoted_message.id AS quote_id,
           quoted_message.direction AS quote_direction,
           quoted_message.message_type AS quote_message_type,
           quoted_message.text AS quote_text,
           quoted_message.file_name AS quote_file_name
      FROM chat_messages AS current_message
      LEFT JOIN chat_messages AS quoted_message
        ON quoted_message.id = current_message.quote_message_id
"""


def utc_now() -> datetime:
    return datetime.now(UTC)


def _dump_time(value: datetime) -> str:
    return value.astimezone(UTC).isoformat(timespec="microseconds")


class SQLiteStore:
    """面向单进程部署的异步存储。

    共享连接上的所有操作都会串行执行，与项目刻意采用的单进程
    部署模型一致，同时保证事务边界明确且结果可预测。
    """

    def __init__(self, path: Path) -> None:
        self._path = path
        self._connection: aiosqlite.Connection | None = None
        self._lock = asyncio.Lock()

    @property
    def is_open(self) -> bool:
        return self._connection is not None

    def _require_connection(self) -> aiosqlite.Connection:
        if self._connection is None:
            raise RuntimeError("SQLiteStore is not open")
        return self._connection

    async def open(self) -> None:
        if self._connection is not None:
            return
        self._path.parent.mkdir(parents=True, exist_ok=True)
        connection = await aiosqlite.connect(self._path)
        connection.row_factory = aiosqlite.Row
        self._connection = connection
        async with self._lock:
            await initialize_schema(
                connection,
                recovery_time=_dump_time(utc_now()),
            )

    async def clear_all(self) -> None:
        """事务性删除全部账号、消息、去重和游标状态，并回收磁盘页。"""

        connection = self._require_connection()
        async with self._lock:
            async with immediate_transaction(connection):
                # 显式列出业务表，新增持久化能力时必须同步扩展此清单和测试。
                for table in BUSINESS_TABLES:
                    await connection.execute(f"DELETE FROM {table}")

            # DELETE 后截断 WAL 并整理主库，避免已清除的敏感载荷继续占用文件页。
            checkpoint = await connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            await checkpoint.close()
            await connection.execute("VACUUM")

    async def close(self) -> None:
        connection = self._connection
        if connection is None:
            return
        async with self._lock:
            await connection.close()
            self._connection = None

    async def get_state(self, key: str) -> str | None:
        connection = self._require_connection()
        async with self._lock:
            cursor = await connection.execute(
                "SELECT value FROM bridge_state WHERE key = ?",
                (key,),
            )
            row = await cursor.fetchone()
            await cursor.close()
        return str(row["value"]) if row else None

    async def set_state(self, key: str, value: str) -> None:
        connection = self._require_connection()
        async with self._lock:
            await connection.execute(
                """
                INSERT INTO bridge_state(key, value) VALUES(?, ?)
                ON CONFLICT(key) DO UPDATE SET value = excluded.value
                """,
                (key, value),
            )
            await connection.commit()

    async def activate_session(self, fingerprint: str) -> bool:
        """激活当前授权会话，并作废旧 token 对应的状态。"""

        connection = self._require_connection()
        async with self._lock:
            async with immediate_transaction(connection):
                cursor = await connection.execute(
                    "SELECT value FROM bridge_state WHERE key = 'active_fingerprint'"
                )
                row = await cursor.fetchone()
                await cursor.close()
                previous = str(row["value"]) if row else None
                changed = previous is not None and previous != fingerprint
                if changed:
                    await connection.execute("DELETE FROM recipients")
                    await connection.execute("DELETE FROM bridge_state WHERE key LIKE 'cursor:%'")
                    now = _dump_time(utc_now())
                    await connection.execute(
                        """
                        UPDATE outbound_messages
                           SET status = ?, last_error_code = ?, updated_at = ?
                         WHERE status = ?
                        """,
                        (
                            OutboundStatus.FAILED.value,
                            "interaction_required",
                            now,
                            OutboundStatus.SENDING.value,
                        ),
                    )
                    await connection.execute(
                        """
                        UPDATE chat_messages
                           SET status = ?, error_code = ?, updated_at = ?
                         WHERE status = ?
                        """,
                        (
                            ConversationStatus.FAILED.value,
                            "interaction_required",
                            now,
                            ConversationStatus.SENDING.value,
                        ),
                    )
                await connection.execute(
                    """
                    INSERT INTO bridge_state(key, value)
                    VALUES('active_fingerprint', ?)
                    ON CONFLICT(key) DO UPDATE SET value = excluded.value
                    """,
                    (fingerprint,),
                )
        return changed

    async def get_active_fingerprint(self) -> str | None:
        return await self.get_state("active_fingerprint")

    async def reconcile_owner_binding(
        self,
        *,
        user_id: str,
        credential_fingerprint: str,
    ) -> bool:
        """校验唯一 owner，并把旧版本按 user_id 保存的上下文迁移为 owner。

        旧版本可能只保存了以 ``user_id`` 为 alias 的记录。本方法只会迁移
        与本次扫码账号和授权指纹都完全一致的记录；发现冲突绑定时会清空
        旧记录，要求扫码账号重新发送消息，避免错误接管 owner。
        """

        connection = self._require_connection()
        async with self._lock:
            async with immediate_transaction(connection):
                cursor = await connection.execute(
                    """
                    SELECT user_id, credential_fingerprint
                      FROM recipients
                     WHERE alias = ?
                    """,
                    (OWNER_ALIAS,),
                )
                owner_row = await cursor.fetchone()
                await cursor.close()
                if owner_row is not None:
                    owner_matches = (
                        str(owner_row["user_id"]) == user_id
                        and str(owner_row["credential_fingerprint"]) == credential_fingerprint
                    )
                    if not owner_matches:
                        await connection.execute("DELETE FROM recipients")
                        return False
                    await connection.execute(
                        "DELETE FROM recipients WHERE alias <> ?",
                        (OWNER_ALIAS,),
                    )
                    return True

                cursor = await connection.execute(
                    """
                    SELECT context_token, updated_at
                      FROM recipients
                     WHERE user_id = ? AND credential_fingerprint = ?
                     ORDER BY updated_at DESC
                     LIMIT 1
                    """,
                    (user_id, credential_fingerprint),
                )
                legacy_row = await cursor.fetchone()
                await cursor.close()
                if legacy_row is None:
                    return False

                await connection.execute(
                    """
                    INSERT INTO recipients(
                        alias, user_id, context_token, credential_fingerprint, updated_at
                    ) VALUES(?, ?, ?, ?, ?)
                    """,
                    (
                        OWNER_ALIAS,
                        user_id,
                        str(legacy_row["context_token"]),
                        credential_fingerprint,
                        str(legacy_row["updated_at"]),
                    ),
                )
                await connection.execute(
                    "DELETE FROM recipients WHERE alias <> ?",
                    (OWNER_ALIAS,),
                )
                return True

    async def save_owner_context(
        self,
        *,
        user_id: str,
        context_token: str,
        credential_fingerprint: str,
    ) -> None:
        """保存唯一 owner 的最新会话上下文，并清理旧版多 alias 记录。"""

        connection = self._require_connection()
        now = _dump_time(utc_now())
        async with self._lock:
            async with immediate_transaction(connection):
                cursor = await connection.execute(
                    """
                    SELECT user_id, credential_fingerprint
                      FROM recipients
                     WHERE alias = ?
                    """,
                    (OWNER_ALIAS,),
                )
                existing = await cursor.fetchone()
                await cursor.close()
                if existing is not None and (
                    str(existing["user_id"]) != user_id
                    or str(existing["credential_fingerprint"]) != credential_fingerprint
                ):
                    raise RuntimeError("Owner binding conflicts with the active WeChat session")

                await connection.execute(
                    """
                    INSERT INTO recipients(
                        alias, user_id, context_token, credential_fingerprint, updated_at
                    ) VALUES(?, ?, ?, ?, ?)
                    ON CONFLICT(alias) DO UPDATE SET
                        context_token = excluded.context_token,
                        updated_at = excluded.updated_at
                    """,
                    (
                        OWNER_ALIAS,
                        user_id,
                        context_token,
                        credential_fingerprint,
                        now,
                    ),
                )
                await connection.execute(
                    "DELETE FROM recipients WHERE alias <> ?",
                    (OWNER_ALIAS,),
                )

    async def resolve_owner(
        self,
        credential_fingerprint: str,
    ) -> OwnerBinding | None:
        connection = self._require_connection()
        async with self._lock:
            cursor = await connection.execute(
                """
                SELECT user_id, context_token, credential_fingerprint, updated_at
                  FROM recipients
                 WHERE alias = ? AND credential_fingerprint = ?
                 LIMIT 1
                """,
                (OWNER_ALIAS, credential_fingerprint),
            )
            row = await cursor.fetchone()
            await cursor.close()
        return owner_from_row(row) if row else None

    async def remember_inbound_event(self, event: InboundEvent) -> bool:
        connection = self._require_connection()
        payload = event.model_dump_json()
        async with self._lock:
            cursor = await connection.execute(
                """
                INSERT OR IGNORE INTO inbound_events(event_id, payload_json, received_at)
                VALUES(?, ?, ?)
                """,
                (event.event_id, payload, _dump_time(utc_now())),
            )
            inserted = cursor.rowcount == 1
            await cursor.close()
            await connection.commit()
        return inserted

    async def record_inbound_conversation(
        self,
        event: InboundEvent,
    ) -> ConversationMessage:
        """把规范化微信入站事件保存为管理页面聊天记录。"""

        connection = self._require_connection()
        occurred_at = _dump_time(event.occurred_at)
        file_name = (
            event.media.file_name
            if event.media is not None
            else event.message.text
            if event.message.type == "file"
            else None
        )
        mime_type = event.media.mime_type if event.media is not None else None
        media_data = event.media.data if event.media is not None else None
        async with self._lock:
            await connection.execute(
                """
                INSERT OR IGNORE INTO chat_messages(
                    id, direction, message_type, text, file_name, mime_type, media,
                    status, origin, error_code, occurred_at, updated_at,
                    native_quote_title, native_quote_type, native_quote_text
                ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, ?, ?, ?, ?, ?)
                """,
                (
                    event.event_id,
                    ConversationDirection.INBOUND.value,
                    event.message.type,
                    event.message.text,
                    file_name,
                    mime_type,
                    media_data,
                    ConversationStatus.RECEIVED.value,
                    MessageOrigin.WECHAT.value,
                    occurred_at,
                    occurred_at,
                    event.quote.title if event.quote is not None else None,
                    event.quote.type if event.quote is not None else None,
                    event.quote.text if event.quote is not None else None,
                ),
            )
            cursor = await connection.execute(
                f"{_CONVERSATION_SELECT_SQL} WHERE current_message.id = ?",
                (event.event_id,),
            )
            row = await cursor.fetchone()
            await cursor.close()
            await connection.commit()
        if row is None:
            raise RuntimeError("Failed to read inbound conversation message")
        return conversation_from_row(row)

    async def begin_outbound(
        self,
        *,
        user_id: str,
        message_type: OutboundMessageType,
        payload: OutboundPayload,
        origin: MessageOrigin,
        quote_message_id: str | None = None,
    ) -> OutboundMessage:
        connection = self._require_connection()
        now = utc_now()
        message_id = str(uuid4())
        async with self._lock:
            async with immediate_transaction(connection):
                await _require_quote_exists(connection, quote_message_id)
                await _insert_outbound_audit(
                    connection,
                    message_id=message_id,
                    user_id=user_id,
                    message_type=message_type,
                    occurred_at=now,
                )
                await _insert_outbound_chat(
                    connection,
                    message_id=message_id,
                    payload=payload,
                    origin=origin,
                    occurred_at=now,
                    quote_message_id=quote_message_id,
                )
                row = await _get_outbound_row(connection, message_id)
        if row is None:
            raise RuntimeError("Failed to read newly created outbound message")
        return outbound_from_row(row)

    async def get_outbound(self, message_id: str) -> OutboundMessage | None:
        connection = self._require_connection()
        async with self._lock:
            cursor = await connection.execute(
                "SELECT * FROM outbound_messages WHERE id = ?",
                (message_id,),
            )
            row = await cursor.fetchone()
            await cursor.close()
        return outbound_from_row(row) if row else None

    async def get_conversation_message(
        self,
        message_id: str,
    ) -> ConversationMessage | None:
        connection = self._require_connection()
        async with self._lock:
            cursor = await connection.execute(
                f"{_CONVERSATION_SELECT_SQL} WHERE current_message.id = ?",
                (message_id,),
            )
            row = await cursor.fetchone()
            await cursor.close()
        return conversation_from_row(row) if row else None

    async def list_conversation_messages(
        self,
        *,
        cursor: str | None,
        limit: int,
    ) -> ConversationMessagePage:
        """从最新消息向前分页，并在每页内按时间正序返回。"""

        if not 1 <= limit <= 100:
            raise ValueError("limit must be between 1 and 100")

        connection = self._require_connection()
        parameters: list[object] = []
        where_sql = ""
        if cursor is not None:
            cursor_occurred_at, cursor_id = decode_conversation_cursor(cursor)
            where_sql = (
                " WHERE (current_message.occurred_at < ?"
                " OR (current_message.occurred_at = ? AND current_message.id < ?))"
            )
            parameters.extend((cursor_occurred_at, cursor_occurred_at, cursor_id))
        parameters.append(limit + 1)

        async with self._lock:
            database_cursor = await connection.execute(
                (
                    f"{_CONVERSATION_SELECT_SQL}{where_sql}"
                    " ORDER BY current_message.occurred_at DESC,"
                    " current_message.id DESC LIMIT ?"
                ),
                tuple(parameters),
            )
            rows = list(await database_cursor.fetchall())
            await database_cursor.close()

        page_rows = rows[:limit]
        descending = tuple(conversation_from_row(row) for row in page_rows)
        next_cursor = (
            encode_conversation_cursor(descending[-1]) if len(rows) > limit and descending else None
        )
        return ConversationMessagePage(
            items=tuple(reversed(descending)),
            next_cursor=next_cursor,
        )

    async def get_conversation_media(
        self,
        message_id: str,
    ) -> ConversationMedia | None:
        connection = self._require_connection()
        async with self._lock:
            cursor = await connection.execute(
                """
                SELECT media, mime_type, file_name
                  FROM chat_messages
                 WHERE id = ? AND media IS NOT NULL
                """,
                (message_id,),
            )
            row = await cursor.fetchone()
            await cursor.close()
        if row is None:
            return None
        return ConversationMedia(
            data=bytes(row["media"]),
            mime_type=str(row["mime_type"] or "application/octet-stream"),
            file_name=str(row["file_name"]) if row["file_name"] is not None else None,
        )

    async def mark_outbound_sent(self, message_id: str) -> OutboundMessage:
        connection = self._require_connection()
        now = _dump_time(utc_now())
        async with self._lock:
            await connection.execute(
                """
                UPDATE outbound_messages
                   SET status = ?, updated_at = ?, sent_at = ?, last_error_code = NULL
                 WHERE id = ?
                """,
                (OutboundStatus.SENT.value, now, now, message_id),
            )
            await connection.execute(
                """
                UPDATE chat_messages
                   SET status = ?, updated_at = ?, error_code = NULL
                 WHERE id = ?
                """,
                (ConversationStatus.SENT.value, now, message_id),
            )
            cursor = await connection.execute(
                "SELECT * FROM outbound_messages WHERE id = ?",
                (message_id,),
            )
            row = await cursor.fetchone()
            await cursor.close()
            await connection.commit()
        if row is None:
            raise RuntimeError("Outbound message disappeared before completion")
        return outbound_from_row(row)

    async def mark_outbound_failed(
        self,
        message_id: str,
        error_code: str,
    ) -> OutboundMessage:
        connection = self._require_connection()
        now = _dump_time(utc_now())
        async with self._lock:
            await connection.execute(
                """
                UPDATE outbound_messages
                   SET status = ?, updated_at = ?, last_error_code = ?
                 WHERE id = ?
                """,
                (
                    OutboundStatus.FAILED.value,
                    now,
                    error_code,
                    message_id,
                ),
            )
            await connection.execute(
                """
                UPDATE chat_messages
                   SET status = ?, updated_at = ?, error_code = ?
                 WHERE id = ?
                """,
                (
                    ConversationStatus.FAILED.value,
                    now,
                    error_code,
                    message_id,
                ),
            )
            cursor = await connection.execute(
                "SELECT * FROM outbound_messages WHERE id = ?",
                (message_id,),
            )
            row = await cursor.fetchone()
            await cursor.close()
            await connection.commit()
        if row is None:
            raise RuntimeError("Outbound message disappeared before failure recording")
        return outbound_from_row(row)


async def _require_quote_exists(
    connection: aiosqlite.Connection,
    quote_message_id: str | None,
) -> None:
    if quote_message_id is None:
        return
    cursor = await connection.execute(
        "SELECT 1 FROM chat_messages WHERE id = ?",
        (quote_message_id,),
    )
    try:
        if await cursor.fetchone() is None:
            raise QuotedMessageNotFoundError("The quoted conversation message was not found")
    finally:
        await cursor.close()


async def _insert_outbound_audit(
    connection: aiosqlite.Connection,
    *,
    message_id: str,
    user_id: str,
    message_type: OutboundMessageType,
    occurred_at: datetime,
) -> None:
    timestamp = _dump_time(occurred_at)
    # 旧数据库的以下兼容列仍有 NOT NULL/UNIQUE 约束，但不再表示外部幂等。
    await connection.execute(
        """
        INSERT INTO outbound_messages(
            id, idempotency_key, payload_hash, recipient, user_id, text,
            message_type, status, attempts, next_attempt_at, created_at, updated_at
        ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            message_id,
            f"internal:{message_id}",
            "",
            OWNER_ALIAS,
            user_id,
            "",
            message_type.value,
            OutboundStatus.SENDING.value,
            1,
            timestamp,
            timestamp,
            timestamp,
        ),
    )


async def _insert_outbound_chat(
    connection: aiosqlite.Connection,
    *,
    message_id: str,
    payload: OutboundPayload,
    origin: MessageOrigin,
    occurred_at: datetime,
    quote_message_id: str | None,
) -> None:
    chat_fields = outbound_chat_fields(payload)
    if chat_fields is None:
        return
    chat_type, text, file_name, mime_type, media = chat_fields
    timestamp = _dump_time(occurred_at)
    await connection.execute(
        """
        INSERT INTO chat_messages(
            id, direction, message_type, text, file_name, mime_type, media,
            status, origin, error_code, occurred_at, updated_at, quote_message_id
        ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, ?, ?, ?)
        """,
        (
            message_id,
            ConversationDirection.OUTBOUND.value,
            chat_type.value,
            text,
            file_name,
            mime_type,
            media,
            ConversationStatus.SENDING.value,
            origin.value,
            timestamp,
            timestamp,
            quote_message_id,
        ),
    )


async def _get_outbound_row(
    connection: aiosqlite.Connection,
    message_id: str,
) -> aiosqlite.Row | None:
    cursor = await connection.execute(
        "SELECT * FROM outbound_messages WHERE id = ?",
        (message_id,),
    )
    try:
        return await cursor.fetchone()
    finally:
        await cursor.close()
