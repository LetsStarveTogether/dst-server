from __future__ import annotations

import asyncio
import os
import sys
from argparse import ArgumentParser, ArgumentTypeError
from collections.abc import Sequence

from logbook import StreamHandler

from dst_server.telemetry import TelemetrySettings

from .service import _validate_external_port, prepare, run


def _external_port(value: str) -> int:
    try:
        return _validate_external_port(int(value))
    except ValueError as error:
        raise ArgumentTypeError(str(error)) from error


def main(argv: Sequence[str] | None = None) -> int:
    parser = ArgumentParser(prog="dst-server")
    commands = parser.add_subparsers(dest="command")
    commands.add_parser("prepare", help="prepare shared cluster files and MODs")
    runner = commands.add_parser("run", help="run one shard container")
    runner.add_argument("--external-port", type=_external_port)
    runner.add_argument("shard", nargs="?", help="shard directory name")
    arguments = parser.parse_args(argv)
    telemetry = TelemetrySettings.model_validate({
        "profile": os.environ.get("DST_SERVER_TELEMETRY_PROFILE", "critical")
    })
    with StreamHandler(sys.stdout, format_string="{record.message}").applicationbound():
        if arguments.command == "prepare":
            asyncio.run(prepare())
            return 0
        shard = arguments.shard if arguments.command == "run" else None
        return asyncio.run(
            run(
                telemetry=telemetry,
                shard=shard,
                external_port=getattr(arguments, "external_port", None),
                update_mods=shard is None,
            )
        )


__all__ = ["main"]
