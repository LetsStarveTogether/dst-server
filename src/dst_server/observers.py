from __future__ import annotations

import asyncio
import os
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING

from logbook import Logger

from .process import Server

if TYPE_CHECKING:
    from .otel import OtelPipeline

OTEL_ENDPOINT_VARIABLES = (
    "OTEL_EXPORTER_OTLP_ENDPOINT",
    "OTEL_EXPORTER_OTLP_LOGS_ENDPOINT",
    "OTEL_EXPORTER_OTLP_METRICS_ENDPOINT",
    "OTEL_EXPORTER_OTLP_TRACES_ENDPOINT",
)
logger = Logger(__name__)


def log_line(prefix: str) -> Callable[[str], None]:
    def write(line: str) -> None:
        logger.info("{prefix}{line}", prefix=prefix, line=line)

    return write


async def forward_console(path: Path, server: Server) -> None:
    descriptor = os.open(path, os.O_RDWR | os.O_NONBLOCK)
    pipe = os.fdopen(descriptor, "rb", buffering=0)
    reader = asyncio.StreamReader()
    protocol = asyncio.StreamReaderProtocol(reader)
    transport, _ = await asyncio.get_running_loop().connect_read_pipe(
        lambda: protocol,
        pipe,
    )
    try:
        while raw_line := await reader.readline():
            command = raw_line.decode(errors="replace").rstrip("\r\n")
            if not command:
                continue
            try:
                result = await server.execute(command)
            except Exception as error:
                logger.exception(
                    "{shard}: console command failed: {error}",
                    shard=server.args.shard,
                    error=error,
                )
                continue
            for line in result.splitlines():
                logger.info("{shard}: {line}", shard=server.args.shard, line=line)
    finally:
        transport.close()


async def print_game_events(server: Server) -> None:
    while (observed := await server.read_game_event()) is not None:
        logger.info(
            "{shard}: DST_EVENT|{event}",
            shard=server.args.shard,
            event=observed.record.model_dump_json(),
        )


def otel_requested() -> bool:
    return os.environ.get("OTEL_SDK_DISABLED", "").casefold() != "true" and any(
        os.environ.get(name) for name in OTEL_ENDPOINT_VARIABLES
    )


def configure_otel(cluster_path: Path) -> OtelPipeline | None:
    if not otel_requested():
        return None

    from .otel import configure_otlp

    return configure_otlp(resource_attributes={"dst.cluster.name": cluster_path.name})


def start_observers(
    servers: tuple[Server, ...],
    pipeline: OtelPipeline | None,
) -> list[asyncio.Task[None]]:
    if pipeline is None:
        return [
            asyncio.create_task(
                print_game_events(server),
                name=f"dst-events-{server.args.shard}",
            )
            for server in servers
        ]

    from .otel import export_game_events

    return [
        asyncio.create_task(
            export_game_events(server, pipeline.logger),
            name=f"dst-otel-{server.args.shard}",
        )
        for server in servers
    ]


__all__ = [
    "configure_otel",
    "forward_console",
    "log_line",
    "start_observers",
]
