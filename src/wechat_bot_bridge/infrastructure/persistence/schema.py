"""SQLite schema、前向兼容迁移与启动恢复。"""

from __future__ import annotations

from typing import Final

import aiosqlite

from ...domain.models import ConversationStatus, OutboundStatus

BUSINESS_TABLES: Final[tuple[str, ...]] = (
    "chat_messages",
    "inbound_events",
    "outbound_messages",
    "recipients",
    "bridge_state",
)

_BASE_SCHEMA_SQL: Final[str] = """
    CREATE TABLE IF NOT EXISTS bridge_state (
        key TEXT PRIMARY KEY,
        value TEXT NOT NULL
    );

    CREATE TABLE IF NOT EXISTS recipients (
        alias TEXT PRIMARY KEY,
        user_id TEXT NOT NULL,
        context_token TEXT NOT NULL,
        credential_fingerprint TEXT NOT NULL,
        updated_at TEXT NOT NULL
    );
    CREATE INDEX IF NOT EXISTS idx_recipients_user_session
        ON recipients(user_id, credential_fingerprint);

    CREATE TABLE IF NOT EXISTS inbound_events (
        event_id TEXT PRIMARY KEY,
        payload_json TEXT NOT NULL,
        received_at TEXT NOT NULL
    );

    CREATE TABLE IF NOT EXISTS outbound_messages (
        id TEXT PRIMARY KEY,
        idempotency_key TEXT NOT NULL UNIQUE,
        payload_hash TEXT NOT NULL,
        recipient TEXT NOT NULL,
        user_id TEXT NOT NULL,
        text TEXT NOT NULL,
        message_type TEXT NOT NULL DEFAULT 'text',
        status TEXT NOT NULL,
        attempts INTEGER NOT NULL DEFAULT 0,
        next_attempt_at TEXT NOT NULL,
        last_error_code TEXT,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        sent_at TEXT
    );
    CREATE INDEX IF NOT EXISTS idx_outbound_history
        ON outbound_messages(created_at DESC, id DESC);
    CREATE INDEX IF NOT EXISTS idx_outbound_status_history
        ON outbound_messages(status, created_at DESC, id DESC);

    CREATE TABLE IF NOT EXISTS chat_messages (
        id TEXT PRIMARY KEY,
        direction TEXT NOT NULL,
        message_type TEXT NOT NULL,
        text TEXT NOT NULL DEFAULT '',
        file_name TEXT,
        mime_type TEXT,
        media BLOB,
        status TEXT NOT NULL,
        origin TEXT NOT NULL,
        error_code TEXT,
        occurred_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        protocol_item_id TEXT,
        protocol_message_id TEXT,
        native_quote_title TEXT,
        native_quote_type TEXT,
        native_quote_text TEXT,
        native_quote_target_id TEXT,
        native_quote_protocol_item_id TEXT
    );
    CREATE INDEX IF NOT EXISTS idx_chat_history
        ON chat_messages(occurred_at DESC, id DESC);
"""


async def initialize_schema(
    connection: aiosqlite.Connection,
    *,
    recovery_time: str,
) -> None:
    """初始化数据库，并把旧版本安全迁移到当前结构。

    调用方必须持有存储连接锁。所有迁移均为可重复执行的前向迁移，不删除
    历史列，以确保已经部署的旧数据库仍可直接升级。
    """

    await _configure_connection(connection)
    await connection.executescript(_BASE_SCHEMA_SQL)
    await _migrate_outbound_messages(connection)
    await _migrate_chat_messages(connection)
    await _recover_interrupted_deliveries(
        connection,
        recovery_time=recovery_time,
    )
    await connection.commit()


async def _configure_connection(connection: aiosqlite.Connection) -> None:
    await connection.execute("PRAGMA journal_mode=WAL")
    await connection.execute("PRAGMA foreign_keys=ON")
    await connection.execute("PRAGMA busy_timeout=5000")
    # 清空账号时覆盖已释放页面，降低正文、媒体或上下文残留在空闲页的风险。
    await connection.execute("PRAGMA secure_delete=ON")


async def _migrate_outbound_messages(connection: aiosqlite.Connection) -> None:
    columns = await _table_columns(connection, "outbound_messages")
    if "message_type" not in columns:
        # 旧版 outbox 只有文字类型；默认值保留其原有语义。
        await connection.execute(
            """
            ALTER TABLE outbound_messages
            ADD COLUMN message_type TEXT NOT NULL DEFAULT 'text'
            """
        )


async def _migrate_chat_messages(connection: aiosqlite.Connection) -> None:
    columns = await _table_columns(connection, "chat_messages")
    additions = {
        "protocol_item_id": "TEXT",
        "protocol_message_id": "TEXT",
        "native_quote_title": "TEXT",
        "native_quote_type": "TEXT",
        "native_quote_text": "TEXT",
        "native_quote_target_id": "TEXT",
        "native_quote_protocol_item_id": "TEXT",
    }
    for column_name, column_type in additions.items():
        if column_name not in columns:
            # 这些名称与类型均来自本模块常量，不包含外部输入。
            await connection.execute(
                f"ALTER TABLE chat_messages ADD COLUMN {column_name} {column_type}"
            )
    # 旧版本的桥接引用列可以保留以兼容已有数据库，但不再读取或写入。
    await connection.execute("DROP INDEX IF EXISTS idx_chat_quote")
    await connection.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_chat_protocol_item
            ON chat_messages(protocol_item_id)
        """
    )
    await connection.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_chat_protocol_message
            ON chat_messages(protocol_message_id)
        """
    )
    await connection.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_chat_quote_protocol_item
            ON chat_messages(native_quote_protocol_item_id)
        """
    )


async def _recover_interrupted_deliveries(
    connection: aiosqlite.Connection,
    *,
    recovery_time: str,
) -> None:
    """把进程退出时未完成的消息标记失败，绝不自动重发。"""

    await connection.execute(
        """
        UPDATE outbound_messages
           SET status = ?, updated_at = ?, last_error_code = ?
         WHERE status IN ('queued', 'retrying', 'sending')
        """,
        (
            OutboundStatus.FAILED.value,
            recovery_time,
            "delivery_interrupted",
        ),
    )
    await connection.execute(
        """
        UPDATE chat_messages
           SET status = ?, updated_at = ?, error_code = ?
         WHERE status = ?
        """,
        (
            ConversationStatus.FAILED.value,
            recovery_time,
            "delivery_interrupted",
            ConversationStatus.SENDING.value,
        ),
    )


async def _table_columns(
    connection: aiosqlite.Connection,
    table: str,
) -> set[str]:
    cursor = await connection.execute(f"PRAGMA table_info({table})")
    try:
        return {str(row["name"]) for row in await cursor.fetchall()}
    finally:
        await cursor.close()
