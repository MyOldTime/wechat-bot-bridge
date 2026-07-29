"""SQLite 可取消安全事务测试。"""

from __future__ import annotations

import asyncio
from pathlib import Path

import aiosqlite
import pytest

from wechat_bot_bridge.infrastructure.persistence.transactions import (
    immediate_transaction,
)


async def test_immediate_transaction_rolls_back_async_cancellation(
    tmp_path: Path,
) -> None:
    connection = await aiosqlite.connect(tmp_path / "transaction.db")
    try:
        await connection.execute("CREATE TABLE records(value TEXT NOT NULL)")
        await connection.commit()

        with pytest.raises(asyncio.CancelledError):
            async with immediate_transaction(connection):
                await connection.execute("INSERT INTO records(value) VALUES('unfinished')")
                raise asyncio.CancelledError

        cursor = await connection.execute("SELECT COUNT(*) FROM records")
        row = await cursor.fetchone()
        await cursor.close()
        assert row is not None
        assert int(row[0]) == 0
    finally:
        await connection.close()
