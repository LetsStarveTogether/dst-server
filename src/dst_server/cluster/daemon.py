import asyncio
import os
import signal
from collections.abc import Awaitable, Callable, Coroutine
from contextlib import AsyncExitStack
from importlib import import_module
from pathlib import Path
from socket import AF_UNIX, SOCK_CLOEXEC, SOCK_DGRAM, SOCK_NONBLOCK, socket
from typing import Any, cast

from logbook import Logger
from pydantic import TypeAdapter

from dst_server.rpc.client import rpc_runtime
from dst_server.rpc.errors import unwrap_outcome
from dst_server.rpc.schema import SCHEMA_FINGERPRINT, load_schema
from dst_server.rpc.servants import (
    AgentServant,
    BootstrapServant,
    WorkerRegistryServant,
)
from dst_server.rpc.transport import (
    INTERNAL_RPC_ADDRESS,
    PUBLIC_RPC_SOCKET,
    abstract_rpc_server,
    filesystem_rpc_server,
)
from dst_server.telemetry import TelemetrySettings

from . import service
from .agent import ShardAgent
from .config import ShardName
from .configuration import ConfigurationStore
from .controller import AgentEndpoint, ClusterController

capnp: Any = import_module("capnp")
logger = Logger(__name__)
RECONNECT_DELAY = 1.0
WATCHDOG_INTERVAL = 60.0
_SHARD_NAME = TypeAdapter(ShardName)

type Shutdown = asyncio.Event
type _Close = Callable[[], Awaitable[object]]
type _Run = Callable[[Shutdown], Coroutine[object, object, None]]


async def serve(
    *,
    shard: str,
    install_path: Path = service.DEFAULT_INSTALL_PATH,
    cluster_path: Path = service.DEFAULT_CLUSTER_PATH,
    external_port: int | None = None,
    telemetry: TelemetrySettings | None = None,
    shutdown: Shutdown | None = None,
) -> int:
    """Run one shard agent until shutdown or an unrecoverable agent failure."""
    return await _daemon(
        lambda event: _run_agent(
            shard,
            install_path,
            cluster_path,
            external_port,
            telemetry,
            event,
        ),
        shutdown,
    )


async def master(
    *,
    install_path: Path = service.DEFAULT_INSTALL_PATH,
    cluster_path: Path = service.DEFAULT_CLUSTER_PATH,
    external_port: int | None = None,
    telemetry: TelemetrySettings | None = None,
    shutdown: Shutdown | None = None,
) -> int:
    """Run the cluster controller and its master shard agent."""
    return await _daemon(
        lambda event: _run_master(
            install_path,
            cluster_path,
            external_port,
            telemetry,
            event,
        ),
        shutdown,
    )


async def _daemon(
    operation: _Run,
    shutdown: Shutdown | None,
) -> int:
    own_shutdown = shutdown is None
    shutdown = shutdown or asyncio.Event()
    if shutdown.is_set():
        return 0
    remove_signals = _install_signal_handlers(shutdown) if own_shutdown else None
    try:
        async with rpc_runtime():
            await _run_with_watchdog(operation, shutdown)
    except Exception as error:
        logger.error(  # ruff: ignore[error-instead-of-exception]
            "DST daemon stopped: {kind}",
            kind=type(error).__name__,
        )
        return 1
    else:
        return 0
    finally:
        if remove_signals is not None:
            remove_signals()


async def _run_with_watchdog(
    operation: _Run,
    shutdown: Shutdown,
) -> None:
    address = os.environ.get("NOTIFY_SOCKET")
    if address is None:
        await operation(shutdown)
        return
    if len(address) <= 1 or address[0] not in "/@" or "\0" in address:
        msg = "NOTIFY_SOCKET must be an absolute path or @abstract address"
        raise ValueError(msg)
    if address.startswith("@"):
        address = "\0" + address[1:]
    with socket(AF_UNIX, SOCK_DGRAM | SOCK_CLOEXEC | SOCK_NONBLOCK) as notifier:
        notifier.connect(address)
        notifier.sendall(b"READY=1")
        async with asyncio.TaskGroup() as group:
            watchdog = group.create_task(_watchdog(notifier), name="dst-watchdog")
            try:
                await operation(shutdown)
            finally:
                watchdog.cancel()


async def _watchdog(notifier: socket) -> None:
    while True:
        await asyncio.sleep(WATCHDOG_INTERVAL)
        notifier.sendall(b"WATCHDOG=1")


async def _run_agent(
    name: str,
    install_path: Path,
    cluster_path: Path,
    external_port: int | None,
    telemetry: TelemetrySettings | None,
    shutdown: Shutdown,
) -> None:
    if shutdown.is_set():
        return
    name = _SHARD_NAME.validate_python(name)
    shards = ConfigurationStore(cluster_path).shards
    selected = next((shard for shard in shards if shard.name == name), None)
    if selected is None:
        msg = f"unknown DST shard: {name}"
        raise ValueError(msg)
    if selected.master:
        msg = "the master shard must use the master command"
        raise ValueError(msg)
    agent = ShardAgent(
        selected,
        install_path=install_path,
        cluster_path=cluster_path,
        external_port=external_port,
        telemetry=telemetry,
    )
    if shutdown.is_set():
        await agent.aclose()
        return
    await _serve_agent(agent, shutdown)


async def _run_master(
    install_path: Path,
    cluster_path: Path,
    external_port: int | None,
    telemetry: TelemetrySettings | None,
    shutdown: Shutdown,
) -> None:
    if shutdown.is_set():
        return
    configuration = ConfigurationStore(cluster_path)
    shards = configuration.shards
    selected = next(shard for shard in shards if shard.master)
    controller = ClusterController(
        configuration,
        install_path=install_path,
    )
    agent = ShardAgent(
        selected,
        install_path=install_path,
        cluster_path=cluster_path,
        external_port=external_port,
        telemetry=telemetry,
    )
    await _serve_master(
        controller,
        agent,
        shutdown,
        public_socket=cluster_path / PUBLIC_RPC_SOCKET.name,
    )


async def _serve_master(
    controller: ClusterController,
    agent: ShardAgent,
    shutdown: Shutdown,
    *,
    internal_address: str = INTERNAL_RPC_ADDRESS,
    public_socket: Path = PUBLIC_RPC_SOCKET,
) -> None:
    stack = AsyncExitStack()
    shutdown_task: asyncio.Task[bool] | None = None
    failure_task: asyncio.Task[None] | None = None
    fatal_task: asyncio.Task[None] | None = None
    try:
        if shutdown.is_set():
            return
        await controller.register(cast(AgentEndpoint, agent))
        await stack.enter_async_context(
            abstract_rpc_server(
                lambda: WorkerRegistryServant(controller),
                internal_address,
            )
        )
        if shutdown.is_set():
            return
        await stack.enter_async_context(
            filesystem_rpc_server(
                public_socket,
                lambda: BootstrapServant(controller),
            )
        )
        if shutdown.is_set():
            return
        shutdown_task = asyncio.create_task(shutdown.wait(), name="dst-shutdown")
        failure_task = asyncio.create_task(
            _report_local_failures(controller, agent),
            name=f"dst-failure-report-{agent.name}",
        )
        fatal_task = asyncio.create_task(
            agent.wait_fatal(),
            name="dst-agent-fatal",
        )
        done, _ = await asyncio.wait(
            {shutdown_task, failure_task, fatal_task},
            return_when=asyncio.FIRST_COMPLETED,
        )
        if shutdown_task in done:
            return
        if failure_task in done:
            failure_task.result()
        if fatal_task in done:
            fatal_task.result()
    finally:
        tasks = tuple(
            task
            for task in (shutdown_task, failure_task, fatal_task)
            if task is not None
        )
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        await _shield_cleanup(
            _best_effort(controller.aclose, stack.aclose, agent.aclose)
        )


async def _report_local_failures(
    controller: ClusterController,
    agent: ShardAgent,
) -> None:
    while True:
        await agent.next_failure()
        await controller.failed(cast(AgentEndpoint, agent))


async def _serve_agent(
    agent: ShardAgent,
    shutdown: Shutdown,
    *,
    internal_address: str = INTERNAL_RPC_ADDRESS,
    reconnect_delay: float = RECONNECT_DELAY,
) -> None:
    if shutdown.is_set():
        await agent.aclose()
        return
    shutdown_task = asyncio.create_task(shutdown.wait(), name="dst-shutdown")
    fatal_task = asyncio.create_task(agent.wait_fatal(), name="dst-agent-fatal")
    cycle: asyncio.Task[None] | None = None
    try:
        while True:
            if shutdown.is_set():
                return
            cycle = asyncio.create_task(
                _registered_cycle(agent, internal_address),
                name=f"dst-registry-{agent.name}",
            )
            done, _ = await asyncio.wait(
                {shutdown_task, fatal_task, cycle},
                return_when=asyncio.FIRST_COMPLETED,
            )
            if shutdown_task in done:
                return
            if fatal_task in done:
                fatal_task.result()
            try:
                cycle.result()
            except Exception as error:
                logger.warning(
                    "Shard registry connection lost: {kind}",
                    kind=type(error).__name__,
                )
            await agent.kill()
            cycle = None
            if await _wait_reconnect(
                reconnect_delay,
                shutdown_task,
                fatal_task,
            ):
                return
    finally:
        await _shield_cleanup(_cleanup_agent(cycle, shutdown_task, fatal_task, agent))


async def _registered_cycle(agent: ShardAgent, internal_address: str) -> None:
    stream: Any | None = None
    client: Any | None = None
    servant = AgentServant(agent)
    disconnected: asyncio.Future[object] | None = None
    failure: asyncio.Task[object] | None = None
    try:
        stream = await capnp.AsyncIoStream.create_unix_connection(
            f"\0{internal_address}"
        )
        client = capnp.TwoPartyClient(stream)
        registry = client.bootstrap().cast_as(load_schema().WorkerRegistry)
        response = await registry.register(
            schemaFingerprint=SCHEMA_FINGERPRINT,
            agent=servant,
        )
        unwrap_outcome(response.result)
        disconnected = asyncio.ensure_future(client.on_disconnect())
        while True:
            failure = asyncio.create_task(
                agent.next_failure(),
                name=f"dst-failure-report-{agent.name}",
            )
            done, _ = await asyncio.wait(
                {disconnected, failure},
                return_when=asyncio.FIRST_COMPLETED,
            )
            if disconnected in done:
                return
            failure.result()
            response = await registry.failed()
            unwrap_outcome(response.result)
            failure = None
    finally:
        for task in (disconnected, failure):
            if task is not None:
                task.cancel()
        await asyncio.gather(
            *(task for task in (disconnected, failure) if task is not None),
            return_exceptions=True,
        )
        await servant.aclose()
        if client is not None:
            client.close()
        if stream is not None:
            stream.close()


async def _wait_reconnect(
    delay: float,
    shutdown: asyncio.Task[bool],
    fatal: asyncio.Task[None],
) -> bool:
    retry = asyncio.create_task(asyncio.sleep(delay), name="dst-registry-retry")
    try:
        done, _ = await asyncio.wait(
            {retry, shutdown, fatal},
            return_when=asyncio.FIRST_COMPLETED,
        )
        if shutdown in done:
            return True
        if fatal in done:
            fatal.result()
        return False
    finally:
        retry.cancel()
        await asyncio.gather(retry, return_exceptions=True)


async def _cleanup_agent(
    cycle: asyncio.Task[None] | None,
    shutdown: asyncio.Task[bool],
    fatal: asyncio.Task[None],
    agent: ShardAgent,
) -> None:
    # Stop while the registry capability is still live, then release the connection.
    await _best_effort(agent.stop)
    if cycle is not None:
        cycle.cancel()
        await asyncio.gather(cycle, return_exceptions=True)
    for task in (shutdown, fatal):
        task.cancel()
    await asyncio.gather(shutdown, fatal, return_exceptions=True)
    await _best_effort(agent.aclose)


async def _best_effort(*operations: _Close) -> None:
    for operation in operations:
        try:
            await operation()
        except Exception as error:
            logger.error(  # ruff: ignore[error-instead-of-exception]
                "Agent cleanup failed: {kind}",
                kind=type(error).__name__,
            )


async def _shield_cleanup(cleanup: Coroutine[object, object, None]) -> None:
    task = asyncio.create_task(cleanup, name="dst-agent-cleanup")
    cancelled = False
    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            cancelled = True
    await task
    if cancelled:
        raise asyncio.CancelledError


def _install_signal_handlers(
    shutdown: Shutdown,
    loop: asyncio.AbstractEventLoop | None = None,
) -> Callable[[], None]:
    loop = loop or asyncio.get_running_loop()
    installed = []
    for number in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(number, shutdown.set)
        except NotImplementedError, RuntimeError:
            continue
        installed.append(number)

    def remove() -> None:
        for number in installed:
            loop.remove_signal_handler(number)

    return remove
