"""命令行入口。"""

from __future__ import annotations

import argparse
from collections.abc import Sequence
from importlib.metadata import version

import uvicorn
from pydantic import ValidationError

from ..core.config import Settings
from ..core.logging import configure_logging
from .app import create_app
from .single_instance import InstanceAlreadyRunningError, single_instance


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="wechat-bot-bridge",
        description="Bridge external HTTP notifications to a WeChat iLink Bot",
    )
    parser.add_argument(
        "--version",
        action="version",
        version=version("wechat-bot-bridge"),
    )
    subcommands = parser.add_subparsers(dest="command", required=True)
    subcommands.add_parser("serve", help="start the HTTP bridge and WeChat listener")
    return parser


def _load_settings(parser: argparse.ArgumentParser) -> Settings:
    try:
        return Settings()
    except ValidationError as exc:
        parser.error(f"invalid configuration: {exc}")


def main(argv: Sequence[str] | None = None) -> None:
    parser = _parser()
    args = parser.parse_args(argv)
    settings = _load_settings(parser)
    configure_logging(settings.log_level)

    if args.command == "serve":
        try:
            with single_instance(settings.data_dir):
                uvicorn.run(
                    create_app(settings),
                    host=settings.host,
                    port=settings.port,
                    log_level=settings.log_level,
                    log_config=None,
                    workers=1,
                )
        except InstanceAlreadyRunningError as exc:
            parser.exit(2, f"{exc}\n")
        return

    parser.error(f"unknown command: {args.command}")


if __name__ == "__main__":
    main()
