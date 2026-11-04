"""单实例锁避免两个进程共享 SQLite 和微信长轮询。"""

from __future__ import annotations

from pathlib import Path

import pytest

from wechat_bot_bridge.bootstrap.single_instance import (
    InstanceAlreadyRunningError,
    single_instance,
)


def test_data_directory_lock_is_exclusive_and_reusable(tmp_path: Path) -> None:
    data_directory = tmp_path / "data"

    with single_instance(data_directory) as lock_path:
        assert lock_path == data_directory / "wechat-bot-bridge.lock"
        assert lock_path.is_file()
        with pytest.raises(InstanceAlreadyRunningError):
            with single_instance(data_directory):
                pass
        # 一次失败的竞争不能误释放第一个持有者的锁。
        with pytest.raises(InstanceAlreadyRunningError):
            with single_instance(data_directory):
                pass

    with single_instance(data_directory):
        pass
