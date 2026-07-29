"""命令行只负责启动常驻服务，扫码入口归管理页面。"""

from __future__ import annotations

import pytest

from wechat_bot_bridge.bootstrap.cli import _parser


def test_cli_no_longer_exposes_interactive_login_subcommand() -> None:
    parser = _parser()

    with pytest.raises(SystemExit) as raised:
        parser.parse_args(["login"])

    assert raised.value.code == 2
    assert parser.parse_args(["serve"]).command == "serve"
