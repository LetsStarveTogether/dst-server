import asyncio
import os
import sys
from argparse import ArgumentParser, ArgumentTypeError
from collections.abc import Sequence

from logbook import StreamHandler

from dst_server.telemetry import TelemetrySettings

from .daemon import heartbeat_is_fresh, master, serve
from .service import _validate_external_port, prepare_shared


def _external_port(value: str) -> int:
    try:
        return _validate_external_port(int(value))
    except ValueError as error:
        raise ArgumentTypeError(str(error)) from error


def main(argv: Sequence[str] | None = None) -> int:
    parser = ArgumentParser(prog="dst-server")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("prepare", help="prepare shared cluster files and MODs")
    commands.add_parser("healthcheck", help="check the daemon heartbeat")
    primary = commands.add_parser("master", help="serve the cluster and master shard")
    primary.add_argument("--external-port", type=_external_port)
    server = commands.add_parser("serve", help="serve one secondary shard")
    server.add_argument("--external-port", type=_external_port)
    server.add_argument("shard", help="shard directory name")
    arguments = parser.parse_args(argv)
    with StreamHandler(sys.stdout, format_string="{record.message}").applicationbound():
        if arguments.command == "prepare":
            asyncio.run(prepare_shared())
            return 0
        if arguments.command == "healthcheck":
            return 0 if heartbeat_is_fresh() else 1
        telemetry = TelemetrySettings.model_validate({
            "profile": os.environ.get("DST_SERVER_TELEMETRY_PROFILE", "critical")
        })
        if arguments.command == "master":
            return asyncio.run(
                master(
                    telemetry=telemetry,
                    external_port=arguments.external_port,
                )
            )
        return asyncio.run(
            serve(
                telemetry=telemetry,
                shard=arguments.shard,
                external_port=arguments.external_port,
            )
        )
