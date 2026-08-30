import asyncio
import math
import os
import signal
import stat
from collections.abc import Awaitable, Callable, Coroutine
from contextlib import AsyncExitStack
from importlib import import_module
from pathlib import Path
from tempfile import NamedTemporaryFile
from time import monotonic_ns
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
DEFAULT_HEARTBEAT_PATH = Path("/run/dst-server/heartbeat")
DEFAULT_HEARTBEAT_INTERVAL = 5.0
DEFAULT_HEARTBEAT_MAX_AGE = 15.0
_MAX_HEARTBEAT_BYTES = 32
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
    heartbeat_path: Path | None = DEFAULT_HEARTBEAT_PATH,
    heartbeat_interval: float = DEFAULT_HEARTBEAT_INTERVAL,
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
        heartbeat_path,
        heartbeat_interval,
    )


async def master(
    *,
    install_path: Path = service.DEFAULT_INSTALL_PATH,
    cluster_path: Path = service.DEFAULT_CLUSTER_PATH,
    external_port: int | None = None,
    telemetry: TelemetrySettings | None = None,
    shutdown: Shutdown | None = None,
    heartbeat_path: Path | None = DEFAULT_HEARTBEAT_PATH,
    heartbeat_interval: float = DEFAULT_HEARTBEAT_INTERVAL,
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
        heartbeat_path,
        heartbeat_interval,
    )


async def _daemon(
    operation: _Run,
    shutdown: Shutdown | None,
    heartbeat_path: Path | None,
    heartbeat_interval: float,
) -> int:
    own_shutdown = shutdown is None
    shutdown = shutdown or asyncio.Event()
    if shutdown.is_set():
        return 0
    remove_signals = _install_signal_handlers(shutdown) if own_shutdown else None
    try:
        async with rpc_runtime():
            if heartbeat_path is None:
                await operation(shutdown)
            else:
                await _run_with_heartbeat(
                    operation,
                    shutdown,
                    heartbeat_path,
                    heartbeat_interval,
                )
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


async def _run_with_heartbeat(
    operation: _Run,
    shutdown: Shutdown,
    path: Path,
    interval: float,
) -> None:
    path = _validate_heartbeat(path, interval)
    _write_heartbeat(path)
    heartbeat = asyncio.create_task(
        _heartbeat(path, interval),
        name="dst-heartbeat",
    )
    runtime = asyncio.create_task(operation(shutdown), name="dst-daemon")
    try:
        done, _ = await asyncio.wait(
            (runtime, heartbeat),
            return_when=asyncio.FIRST_COMPLETED,
        )
        if runtime in done:
            await runtime
        else:
            heartbeat.result()
    finally:
        for task in (runtime, heartbeat):
            task.cancel()
        await asyncio.gather(runtime, heartbeat, return_exceptions=True)
        path.unlink(missing_ok=True)


async def _heartbeat(path: Path, interval: float) -> None:
    while True:
        await asyncio.sleep(interval)
        _write_heartbeat(path)


def _validate_heartbeat(path: Path, interval: float) -> Path:
    if not path.is_absolute():
        msg = "heartbeat path must be absolute"
        raise ValueError(msg)
    if not math.isfinite(interval) or interval <= 0:
        msg = "heartbeat interval must be a positive finite number"
        raise ValueError(msg)
    parent = path.parent
    parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    metadata = parent.lstat()
    if (
        not stat.S_ISDIR(metadata.st_mode)
        or metadata.st_uid != os.geteuid()
        or metadata.st_mode & 0o022
    ):
        msg = f"heartbeat directory must be owner-controlled: {parent}"
        raise PermissionError(msg)
    return path


def _write_heartbeat(path: Path) -> None:
    temporary: Path | None = None
    try:
        with NamedTemporaryFile(
            "w",
            encoding="ascii",
            prefix=f".{path.name}.",
            dir=path.parent,
            delete=False,
        ) as stream:
            temporary = Path(stream.name)
            stream.write(str(monotonic_ns()))
            os.fchmod(stream.fileno(), 0o600)
        temporary.replace(path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def heartbeat_is_fresh(
    path: Path = DEFAULT_HEARTBEAT_PATH,
    max_age: float = DEFAULT_HEARTBEAT_MAX_AGE,
) -> bool:
    if not math.isfinite(max_age) or max_age <= 0:
        msg = "heartbeat max age must be a positive finite number"
        raise ValueError(msg)
    descriptor: int | None = None
    try:
        descriptor = os.open(
            path,
            os.O_RDONLY | os.O_CLOEXEC | os.O_NONBLOCK | getattr(os, "O_NOFOLLOW", 0),
        )
        metadata = os.fstat(descriptor)
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_uid != os.geteuid()
            or metadata.st_size > _MAX_HEARTBEAT_BYTES
        ):
            return False
        value = int(os.read(descriptor, 33))
    except OSError, ValueError:
        return False
    finally:
        if descriptor is not None:
            os.close(descriptor)
    age = monotonic_ns() - value
    return 0 <= age <= max_age * 1_000_000_000


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
