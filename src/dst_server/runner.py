from __future__ import annotations

import asyncio
import os
import signal
import sys
from contextlib import suppress
from pathlib import Path

from logbook import Logger, StreamHandler

from .arguments import ServerArgs
from .cluster import Shard, discover_shards, ensure_fifo, prepare_cluster
from .mods import prepare_mods, update_server_mods
from .observers import configure_otel, forward_console, log_line, start_observers
from .process import Server

DEFAULT_INSTALL_PATH = Path("/install")
DEFAULT_CLUSTER_PATH = Path("/cluster")
EXECUTABLE = Path("bin64/dontstarve_dedicated_server_nullrenderer_x64")
PROXY_URL = "socks5://127.0.0.1:1080"
logger = Logger(__name__)


def build_server(
    shard: Shard,
    *,
    executable: Path,
    cluster_path: Path,
) -> Server:
    return Server(
        ServerArgs(
            shard=shard.name,
            executable=executable,
            persistent_storage_root=cluster_path.parent,
            conf_dir=".",
            cluster=cluster_path.name,
            ugc_directory=cluster_path / "mods" / "ugc",
        ),
        log_handler=log_line(f"{shard.name}: "),
    )


async def prepare_servers(
    install_path: Path,
    cluster_path: Path,
    *,
    update_mods: bool,
) -> tuple[tuple[Shard, ...], tuple[Server, ...]]:
    executable = install_path / EXECUTABLE
    if not executable.is_file():
        raise FileNotFoundError(executable)
    prepare_cluster(cluster_path)
    shards = discover_shards(cluster_path)
    mod_ids = prepare_mods(install_path, cluster_path)
    logger.info(
        "Found {shards} shard(s) and {mods} Workshop mod(s).",
        shards=len(shards),
        mods=len(mod_ids),
    )
    if update_mods and mod_ids:
        proxy_url = PROXY_URL if (cluster_path / "mods" / "proxy").is_file() else None
        await update_server_mods(
            executable,
            cluster_path / "mods" / "ugc",
            proxy_url=proxy_url,
            log_handler=log_line("[MOD_UPDATE]: "),
        )
    for shard in shards:
        ensure_fifo(shard.console_path)
    servers = tuple(
        build_server(shard, executable=executable, cluster_path=cluster_path)
        for shard in shards
    )
    return shards, servers


async def start_servers(servers: tuple[Server, ...]) -> None:
    async with asyncio.TaskGroup() as tasks:
        for server in servers:
            tasks.create_task(server.start())


async def stop_server(server: Server) -> None:
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


async def stop_servers(servers: tuple[Server, ...]) -> None:
    await asyncio.gather(
        *(stop_server(server) for server in servers),
        return_exceptions=True,
    )


async def wait_for_start(
    servers: tuple[Server, ...],
    signal_task: asyncio.Task[bool],
) -> bool:
    start_task = asyncio.create_task(start_servers(servers), name="dst-start")
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
        shard=server.args.shard,
        returncode=returncode,
    )
    return returncode or 1


async def serve(
    shards: tuple[Shard, ...],
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
        background = start_observers(servers, pipeline)
        background.extend(
            asyncio.create_task(
                forward_console(shard.console_path, server),
                name=f"dst-console-{shard.name}",
            )
            for shard, server in zip(shards, servers, strict=True)
        )
        wait_tasks = {
            asyncio.create_task(
                server.wait(),
                name=f"dst-wait-{server.args.shard}",
            ): server
            for server in servers
        }
        logger.info("All DST shards are ready.")
        return await wait_for_exit(wait_tasks, signal_task)
    finally:
        await stop_servers(servers)
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
    shutdown: asyncio.Event | None = None,
) -> int:
    shards, servers = await prepare_servers(
        install_path,
        cluster_path,
        update_mods=update_mods,
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


def main() -> int:
    install_path = Path(os.environ.get("DST_INSTALL_PATH", DEFAULT_INSTALL_PATH))
    cluster_path = Path(os.environ.get("DST_CLUSTER_PATH", DEFAULT_CLUSTER_PATH))
    skip_mod_update = os.environ.get("DST_SKIP_MOD_UPDATE", "").casefold() in {
        "1",
        "true",
        "yes",
    }
    handler = StreamHandler(sys.stdout, format_string="{record.message}")
    with handler.applicationbound():
        try:
            return asyncio.run(
                run(
                    install_path=install_path,
                    cluster_path=cluster_path,
                    update_mods=not skip_mod_update,
                )
            )
        except Exception as error:
            logger.exception("Error: {error}", error=error)
            return 1


__all__ = ["main", "run"]
