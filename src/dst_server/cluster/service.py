from __future__ import annotations

import asyncio
import os
import signal
from collections.abc import Callable
from contextlib import suppress
from pathlib import Path
from typing import TYPE_CHECKING

from logbook import Logger

from dst_server.runtime import Server, ServerConfig
from dst_server.telemetry import TelemetrySettings

from . import console, layout, mods

if TYPE_CHECKING:
    from opentelemetry._logs import (
        Logger as OtelLogger,
    )

    from dst_server.telemetry.otel import Pipeline

DEFAULT_INSTALL_PATH = Path("/install")
DEFAULT_CLUSTER_PATH = Path("/cluster")
EXECUTABLE = Path("bin64/dontstarve_dedicated_server_nullrenderer_x64")
PROXY_URL = "socks5://127.0.0.1:1080"
OTEL_ENDPOINTS = (
    "OTEL_EXPORTER_OTLP_ENDPOINT",
    "OTEL_EXPORTER_OTLP_LOGS_ENDPOINT",
    "OTEL_EXPORTER_OTLP_METRICS_ENDPOINT",
    "OTEL_EXPORTER_OTLP_TRACES_ENDPOINT",
)
logger = Logger(__name__)


def log_handler(prefix: str) -> Callable[[str], None]:
    def write(line: str) -> None:
        logger.info("{prefix}{line}", prefix=prefix, line=line)

    return write


async def prepare(
    install_path: Path,
    cluster_path: Path,
    *,
    update_mods: bool,
    telemetry: TelemetrySettings | None = None,
) -> tuple[tuple[layout.Shard, ...], tuple[Server, ...]]:
    executable = install_path / EXECUTABLE
    if not executable.is_file():
        raise FileNotFoundError(executable)
    layout.prepare(cluster_path)
    shards = layout.discover(cluster_path)
    mod_ids = mods.prepare(install_path, cluster_path)
    logger.info(
        "Found {shards} shard(s) and {mods} Workshop mod(s).",
        shards=len(shards),
        mods=len(mod_ids),
    )
    if update_mods and mod_ids:
        proxy_url = PROXY_URL if (cluster_path / "mods" / "proxy").is_file() else None
        await mods.update(
            executable,
            cluster_path / "mods" / "ugc",
            proxy_url=proxy_url,
            log_handler=log_handler("[MOD_UPDATE]: "),
        )
    for shard in shards:
        console.ensure(shard.console)
    telemetry = telemetry if telemetry is not None else TelemetrySettings()
    servers = tuple(
        Server(
            ServerConfig(
                shard=shard.name,
                executable=executable,
                persistent_storage_root=cluster_path.parent,
                conf_dir=".",
                cluster=cluster_path.name,
                ugc_directory=cluster_path / "mods" / "ugc",
                telemetry=telemetry,
            ),
            log_handler=log_handler(f"{shard.name}: "),
        )
        for shard in shards
    )
    return shards, servers


async def start(servers: tuple[Server, ...]) -> None:
    async with asyncio.TaskGroup() as tasks:
        for server in servers:
            tasks.create_task(server.start())


async def stop_one(server: Server) -> None:
    try:
        process = server.process
    except RuntimeError:
        return
    if process.returncode is not None:
        await server.wait()
        return
    try:
        await server.stop()
    except TimeoutError:
        await server.kill()


async def stop(servers: tuple[Server, ...]) -> None:
    await asyncio.gather(
        *(stop_one(server) for server in servers),
        return_exceptions=True,
    )


async def wait_for_start(
    servers: tuple[Server, ...],
    signal_task: asyncio.Task[bool],
) -> bool:
    start_task = asyncio.create_task(start(servers), name="dst-start")
    done, _ = await asyncio.wait(
        (start_task, signal_task),
        return_when=asyncio.FIRST_COMPLETED,
    )
    if signal_task not in done:
        await start_task
        return True
    start_task.cancel()
    await asyncio.gather(start_task, return_exceptions=True)
    return False


async def wait_for_exit(
    wait_tasks: dict[asyncio.Task[int], Server],
    signal_task: asyncio.Task[bool],
) -> int:
    done, _ = await asyncio.wait(
        (*wait_tasks, signal_task),
        return_when=asyncio.FIRST_COMPLETED,
    )
    if signal_task in done:
        return 0
    exited = next(task for task in done if task in wait_tasks)
    server = wait_tasks[exited]
    returncode = exited.result()
    logger.error(
        "shard {shard} exited unexpectedly with status {returncode}",
        shard=server.config.shard,
        returncode=returncode,
    )
    return returncode or 1


def otel_requested() -> bool:
    return os.environ.get("OTEL_SDK_DISABLED", "").casefold() != "true" and any(
        os.environ.get(name) for name in OTEL_ENDPOINTS
    )


def configure_otel(cluster_path: Path) -> Pipeline | None:
    if not otel_requested():
        return None

    try:
        from dst_server.telemetry.otel import configure

        return configure(resource_attributes={"dst.cluster.name": cluster_path.name})
    except Exception:
        logger.exception("failed to configure OpenTelemetry; using local event logging")
        return None


async def log_events(server: Server) -> None:
    while (observed := await server.read_game_event()) is not None:
        logger.info(
            "{shard}: DST_EVENT|{event}",
            shard=server.config.shard,
            event=observed.record.model_dump_json(),
        )


async def export_events(server: Server, logger: OtelLogger) -> None:
    from dst_server.telemetry.otel import emit

    while (observed := await server.read_game_event()) is not None:
        attributes = {
            "dst.cluster.name": server.config.cluster,
            "dst.shard.name": server.config.shard,
        }
        if server.session_id is not None:
            attributes["dst.session.id"] = server.session_id
        emit(logger, observed, attributes=attributes)


def start_event_tasks(
    servers: tuple[Server, ...],
    pipeline: Pipeline | None,
) -> list[asyncio.Task[None]]:
    if pipeline is None:
        return [
            asyncio.create_task(
                log_events(server),
                name=f"dst-events-{server.config.shard}",
            )
            for server in servers
        ]
    return [
        asyncio.create_task(
            export_events(server, pipeline.logger),
            name=f"dst-otel-{server.config.shard}",
        )
        for server in servers
    ]


async def serve(
    shards: tuple[layout.Shard, ...],
    servers: tuple[Server, ...],
    cluster_path: Path,
    shutdown: asyncio.Event,
) -> int:
    pipeline = configure_otel(cluster_path)
    background: list[asyncio.Task[None]] = []
    wait_tasks: dict[asyncio.Task[int], Server] = {}
    signal_task = asyncio.create_task(shutdown.wait(), name="dst-shutdown-signal")
    try:
        if not await wait_for_start(servers, signal_task):
            return 0
        background = start_event_tasks(servers, pipeline)
        background.extend(
            asyncio.create_task(
                console.forward(shard.console, server),
                name=f"dst-console-{shard.name}",
            )
            for shard, server in zip(shards, servers, strict=True)
        )
        wait_tasks = {
            asyncio.create_task(
                server.wait(),
                name=f"dst-wait-{server.config.shard}",
            ): server
            for server in servers
        }
        logger.info("All DST shards are ready.")
        return await wait_for_exit(wait_tasks, signal_task)
    finally:
        await stop(servers)
        signal_task.cancel()
        for task in (*background, *wait_tasks):
            if not task.done():
                task.cancel()
        await asyncio.gather(
            signal_task,
            *background,
            *wait_tasks,
            return_exceptions=True,
        )
        if pipeline is not None:
            await pipeline.shutdown()


async def run(
    *,
    install_path: Path = DEFAULT_INSTALL_PATH,
    cluster_path: Path = DEFAULT_CLUSTER_PATH,
    update_mods: bool = True,
    telemetry: TelemetrySettings | None = None,
    shutdown: asyncio.Event | None = None,
) -> int:
    shards, servers = await prepare(
        install_path,
        cluster_path,
        update_mods=update_mods,
        telemetry=telemetry,
    )
    loop = asyncio.get_running_loop()
    own_shutdown = shutdown is None
    shutdown = shutdown or asyncio.Event()
    if own_shutdown:
        for value in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(value, shutdown.set)

    try:
        return await serve(shards, servers, cluster_path, shutdown)
    finally:
        if own_shutdown:
            for value in (signal.SIGINT, signal.SIGTERM):
                with suppress(RuntimeError):
                    loop.remove_signal_handler(value)


__all__ = ["prepare", "run"]
