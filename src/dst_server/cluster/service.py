from __future__ import annotations

import asyncio
import os
import signal
from collections.abc import Callable
from contextlib import suppress
from functools import partial
from pathlib import Path
from typing import TYPE_CHECKING, Never

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
CLUSTER_NAME_ENV = "DST_SERVER_CLUSTER_NAME"
MIN_EXTERNAL_PORT = 1024
MAX_EXTERNAL_PORT = 65535
logger = Logger(__name__)

type ShardLogHandler = Callable[[str, str], None]


def _validate_external_port(value: int) -> int:
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or not MIN_EXTERNAL_PORT <= value <= MAX_EXTERNAL_PORT
    ):
        msg = "external_port must be between 1024 and 65535"
        raise ValueError(msg)
    return value


def log_handler(prefix: str) -> Callable[[str], None]:
    def write(line: str) -> None:
        logger.info("{prefix}{line}", prefix=prefix, line=line)

    return write


async def prepare(
    install_path: Path = DEFAULT_INSTALL_PATH,
    cluster_path: Path = DEFAULT_CLUSTER_PATH,
    *,
    update_mods: bool = True,
    shard: str | None = None,
    external_port: int | None = None,
    telemetry: TelemetrySettings | None = None,
    shard_log_handler: ShardLogHandler | None = None,
) -> tuple[tuple[layout.Shard, ...], tuple[Server, ...]]:
    install_path, cluster_path = install_path.resolve(), cluster_path.resolve()  # ruff: ignore[blocking-path-method-in-async-function]
    executable = install_path / EXECUTABLE
    if not executable.is_file():
        raise FileNotFoundError(executable)
    discovered = layout.discover(cluster_path)
    shards = discovered
    if shard is not None:
        shards = tuple(value for value in discovered if value.name == shard)
        if not shards:
            msg = f"unknown DST shard: {shard!r}"
            raise ValueError(msg)
    if external_port is not None:
        external_port = _validate_external_port(external_port)
        if len(shards) != 1:
            msg = "external_port requires exactly one shard"
            raise ValueError(msg)
    layout.prepare(cluster_path)
    mod_ids = mods.prepare(install_path, cluster_path)
    logger.info(
        "Found {shards} shard(s) and {mods} Workshop mod(s).",
        shards=len(shards),
        mods=len(mod_ids),
    )
    setup = cluster_path / "mods" / "dedicated_server_mods_setup.lua"
    if update_mods and (mod_ids or mods.has_setup_code(setup)):
        proxy_url = PROXY_URL if (cluster_path / "mods" / "proxy").is_file() else None
        await mods.update(
            executable,
            cluster_path / "mods" / "ugc",
            proxy_url=proxy_url,
            log_handler=log_handler("[MOD_UPDATE]: "),
        )
    for selected in shards:
        console.ensure(selected.console)
    telemetry = telemetry if telemetry is not None else TelemetrySettings()
    telemetry_cluster = os.environ.get(CLUSTER_NAME_ENV) or cluster_path.name
    servers = tuple(
        Server(
            ServerConfig(
                shard=selected.name,
                executable=executable,
                persistent_storage_root=cluster_path.parent,
                conf_dir=".",
                cluster=cluster_path.name,
                telemetry_cluster=telemetry_cluster,
                ugc_directory=cluster_path / "mods" / "ugc",
                extra_args=(
                    "-skip_update_server_mods",
                    "-external_port",
                    str(external_port),
                )
                if external_port is not None
                else ("-skip_update_server_mods",),
                telemetry=telemetry,
            ),
            log_handler=(
                partial(shard_log_handler, selected.name)
                if shard_log_handler is not None
                else log_handler(f"{selected.name}: ")
            ),
        )
        for selected in shards
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
    results = await asyncio.gather(
        *(stop_one(server) for server in servers),
        return_exceptions=True,
    )
    raise_failures([result for result in results if isinstance(result, BaseException)])


def raise_failures(failures: list[BaseException]) -> None:
    if not failures:
        return
    if len(failures) == 1:
        raise failures[0]
    message = "DST service failed"
    raise BaseExceptionGroup(message, failures)


async def cancel_tasks(
    failures: list[BaseException],
    *tasks: asyncio.Task[object],
) -> None:
    for task in tasks:
        task.cancel()
    while True:
        try:
            results = await asyncio.gather(*tasks, return_exceptions=True)
        except BaseException as error:
            if not any(error is failure for failure in failures):
                failures.append(error)
        else:
            failures.extend(
                result
                for result in results
                if isinstance(result, BaseException)
                and not isinstance(result, asyncio.CancelledError)
                and not any(result is failure for failure in failures)
            )
            return


def raise_background_failure(task: asyncio.Task[None]) -> Never:
    task.result()
    msg = f"critical background task stopped unexpectedly: {task.get_name()}"
    raise RuntimeError(msg)


async def wait_for_start(
    servers: tuple[Server, ...],
    signal_task: asyncio.Task[bool],
    background: list[asyncio.Task[None]],
) -> bool:
    start_task = asyncio.create_task(start(servers), name="dst-start")
    failures: list[BaseException] = []
    result = False
    try:  # ruff:ignore[too-many-statements-in-try-clause]
        done, _ = await asyncio.wait(
            (start_task, signal_task, *background),
            return_when=asyncio.FIRST_COMPLETED,
        )
        if start_task in done:
            await start_task
        failed = next((task for task in background if task in done), None)
        if failed is not None:
            raise_background_failure(failed)
        result = signal_task not in done
    except BaseException as error:
        failures.append(error)
    await cancel_tasks(failures, start_task)
    raise_failures(failures)
    return result


async def wait_for_exit(
    wait_tasks: dict[asyncio.Task[int], Server],
    signal_task: asyncio.Task[bool],
    background: list[asyncio.Task[None]],
) -> int:
    done, _ = await asyncio.wait(
        (*wait_tasks, signal_task, *background),
        return_when=asyncio.FIRST_COMPLETED,
    )
    exited = next((task for task in done if task in wait_tasks), None)
    if exited is not None:
        server = wait_tasks[exited]
        returncode = exited.result()
        logger.error(
            "shard {shard} exited unexpectedly with status {returncode}",
            shard=server.config.shard,
            returncode=returncode,
        )
        return returncode or 1
    failed = next((task for task in background if task in done), None)
    if failed is not None:
        raise_background_failure(failed)
    return 0


def otel_requested() -> bool:
    return os.environ.get("OTEL_SDK_DISABLED", "").casefold() != "true" and any(
        os.environ.get(name) for name in OTEL_ENDPOINTS
    )


def configure_otel(config: ServerConfig) -> Pipeline | None:
    if not otel_requested():
        return None

    try:
        from dst_server.telemetry.otel import configure

        name = config.telemetry_cluster or config.cluster
        return configure(resource_attributes={"dst.cluster.name": name})
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

    attributes = server.recorder.attributes()
    while (observed := await server.read_game_event()) is not None:
        emit(logger, observed, attributes=attributes)


def start_event_tasks(
    servers: tuple[Server, ...],
    pipeline: Pipeline | None,
) -> list[asyncio.Task[None]]:
    logger = pipeline.logger if pipeline is not None else None
    if logger is None:
        return [
            asyncio.create_task(
                log_events(server),
                name=f"dst-events-{server.config.shard}",
            )
            for server in servers
        ]
    return [
        asyncio.create_task(
            export_events(server, logger),
            name=f"dst-otel-{server.config.shard}",
        )
        for server in servers
    ]


async def serve(
    shards: tuple[layout.Shard, ...],
    servers: tuple[Server, ...],
    shutdown: asyncio.Event,
    *,
    ready: asyncio.Event | None = None,
    consume_game_events: bool = True,
) -> int:
    pipeline = configure_otel(servers[0].config) if consume_game_events else None
    background: list[asyncio.Task[None]] = []
    wait_tasks: dict[asyncio.Task[int], Server] = {}
    signal_task = asyncio.create_task(shutdown.wait(), name="dst-shutdown-signal")
    failures: list[BaseException] = []

    async def run_until_exit() -> int:
        if consume_game_events:
            background.extend(start_event_tasks(servers, pipeline))
        if not await wait_for_start(servers, signal_task, background):
            return 0
        background.extend(
            asyncio.create_task(
                console.forward(shard.console, server),
                name=f"dst-console-{shard.name}",
            )
            for shard, server in zip(shards, servers, strict=True)
        )
        wait_tasks.update({
            asyncio.create_task(
                server.wait(),
                name=f"dst-wait-{server.config.shard}",
            ): server
            for server in servers
        })
        logger.info("All DST shards are ready.")
        if ready is not None:
            ready.set()
        return await wait_for_exit(wait_tasks, signal_task, background)

    async def cleanup() -> None:
        try:
            await stop(servers)
        except BaseException as error:
            failures.append(error)

        await cancel_tasks(failures, signal_task, *background, *wait_tasks)

        if pipeline is not None:
            try:
                await pipeline.shutdown()
            except BaseException as error:
                failures.append(error)

    try:
        result = await run_until_exit()
    except BaseException as error:
        failures.append(error)
        result = 0

    await cleanup()
    raise_failures(failures)
    return result


async def run(
    *,
    install_path: Path = DEFAULT_INSTALL_PATH,
    cluster_path: Path = DEFAULT_CLUSTER_PATH,
    update_mods: bool = True,
    shard: str | None = None,
    external_port: int | None = None,
    telemetry: TelemetrySettings | None = None,
    shutdown: asyncio.Event | None = None,
) -> int:
    loop = asyncio.get_running_loop()
    own_shutdown = shutdown is None
    shutdown = shutdown or asyncio.Event()
    if own_shutdown:
        for value in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(value, shutdown.set)

    try:
        if shutdown.is_set():
            return 0
        install_path, cluster_path = install_path.resolve(), cluster_path.resolve()  # ruff: ignore[blocking-path-method-in-async-function]
        prepare_task = asyncio.create_task(
            prepare(
                install_path,
                cluster_path,
                update_mods=update_mods,
                shard=shard,
                external_port=external_port,
                telemetry=telemetry,
            ),
            name="dst-prepare",
        )
        prepare_shutdown = asyncio.create_task(
            shutdown.wait(),
            name="dst-prepare-shutdown",
        )
        prepared: tuple[tuple[layout.Shard, ...], tuple[Server, ...]] | None = None
        failures: list[BaseException] = []
        try:
            done, _ = await asyncio.wait(
                (prepare_task, prepare_shutdown),
                return_when=asyncio.FIRST_COMPLETED,
            )
            if prepare_shutdown not in done:
                prepared = prepare_task.result()
        except BaseException as error:
            failures.append(error)
        await cancel_tasks(failures, prepare_task, prepare_shutdown)
        raise_failures(failures)
        if prepared is None or shutdown.is_set():
            return 0
        shards, servers = prepared
        return await serve(shards, servers, shutdown)
    finally:
        if own_shutdown:
            for value in (signal.SIGINT, signal.SIGTERM):
                with suppress(RuntimeError):
                    loop.remove_signal_handler(value)


__all__ = ["prepare", "run"]
