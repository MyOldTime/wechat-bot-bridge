"""SQLite 事务边界工具。"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import aiosqlite


@asynccontextmanager
async def immediate_transaction(
    connection: aiosqlite.Connection,
) -> AsyncIterator[None]:
    """执行可取消安全的 ``BEGIN IMMEDIATE`` 事务。

    ``asyncio.CancelledError`` 属于 ``BaseException``。使用上下文管理器统一
    捕获并回滚，避免服务关闭恰好发生在写事务中时污染后续连接状态。
    """

    await connection.execute("BEGIN IMMEDIATE")
    try:
        yield
    except BaseException:
        await connection.rollback()
        raise
    else:
        await connection.commit()
