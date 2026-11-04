"""用持久化目录中的文件锁约束单实例运行。"""

from __future__ import annotations

import os
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from importlib import import_module
from pathlib import Path
from typing import BinaryIO, Protocol, cast


class _WindowsLocking(Protocol):
    LK_NBLCK: int
    LK_UNLCK: int

    def locking(self, file_descriptor: int, mode: int, byte_count: int) -> None: ...


class _PosixLocking(Protocol):
    LOCK_EX: int
    LOCK_NB: int
    LOCK_UN: int

    def flock(self, file_descriptor: int, operation: int) -> None: ...


def _windows_locking() -> _WindowsLocking:
    return cast(_WindowsLocking, import_module("msvcrt"))


def _posix_locking() -> _PosixLocking:
    return cast(_PosixLocking, import_module("fcntl"))


class InstanceAlreadyRunningError(RuntimeError):
    """同一持久化目录已经被另一个 Bridge 进程占用。"""


def _acquire_file_lock(stream: BinaryIO) -> None:
    if sys.platform == "win32":
        msvcrt = _windows_locking()
        stream.seek(0, os.SEEK_END)
        if stream.tell() == 0:
            stream.write(b"\0")
            stream.flush()
        stream.seek(0)
        msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
        return

    fcntl = _posix_locking()
    fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)


def _release_file_lock(stream: BinaryIO) -> None:
    try:
        if sys.platform == "win32":
            msvcrt = _windows_locking()
            stream.seek(0)
            msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
            return

        fcntl = _posix_locking()
        fcntl.flock(stream.fileno(), fcntl.LOCK_UN)
    except OSError:
        # 进程退出时操作系统仍会释放句柄；这里不掩盖服务本身的关闭结果。
        return


@contextmanager
def single_instance(data_directory: Path) -> Iterator[Path]:
    """在指定部署数据目录上持有独占锁，退出时由操作系统可靠释放。"""

    data_directory.mkdir(parents=True, exist_ok=True)
    lock_path = data_directory / "wechat-bot-bridge.lock"
    stream = lock_path.open("a+b")
    acquired = False
    try:
        try:
            _acquire_file_lock(stream)
            acquired = True
        except OSError as exc:
            raise InstanceAlreadyRunningError("wechat-bot-bridge 已在使用当前数据目录运行") from exc
        yield lock_path
    finally:
        if acquired:
            _release_file_lock(stream)
        stream.close()
