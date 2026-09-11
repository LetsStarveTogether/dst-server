import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager, nullcontext
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock, Mock

import pytest
from ulid import ULID

from dst_server import commands as c
from dst_server.cluster import daemon
from dst_server.cluster.subscriptions import Broadcast, Subscription
from dst_server.configuration.files import Shard
from dst_server.configuration.store import ConfigurationStore
from dst_server.models.cluster import (
    ClusterStatus,
    GameEventRecord,
    LifecycleRecord,
    LogRecord,
    ShardDesired,
    ShardPhase,
    ShardRuntimeStatus,
)
from dst_server.rpc import ClusterClient, rpc_runtime
from dst_server.rpc.servants import WorkerRegistryServant
from dst_server.rpc.transport import abstract_rpc_server
from tests.helpers import wait_for_event


class AgentStub:
    def __init__(
        self,
        calls: list[str],
        *,
        master: bool = False,
        kill_error: str | None = None,
    ) -> None:
        self.name = "Master" if master else "Caves"
        self.master = master
        self.incarnation = str(ULID())
        self.calls = calls
        self.logs = Broadcast[LogRecord]()
        self.lifecycle = Broadcast[LifecycleRecord]()
        self.game_events = Broadcast[GameEventRecord]()
        self.failures: asyncio.Queue[object] = asyncio.Queue()
        self.failure_waiting = asyncio.Event()
        self.fatal = asyncio.Event()
        self.kill_error = kill_error

    async def runtime_status(self) -> ShardRuntimeStatus:
        return ShardRuntimeStatus(
            name=self.name,
            is_master=self.master,
            desired=ShardDesired.RUNNING,
            phase=ShardPhase.STOPPED,
            agent_incarnation=ULID.from_str(self.incarnation),
            telemetry_profile="critical",
        )

    async def invoke[T](self, command: c.Request[T]) -> T:
        assert isinstance(command, c.Status)
        return cast(T, await self.runtime_status())

    async def next_failure(self) -> object:
        self.failure_waiting.set()
        return await self.failures.get()

    async def wait_fatal(self) -> None:
        await self.fatal.wait()
        msg = "expected fatal agent failure"
        raise RuntimeError(msg)

    async def stop(self) -> None:
        self.calls.append("agent.stop")

    async def kill(self) -> None:
        self.calls.append("agent.kill")
        if self.kill_error:
            raise RuntimeError(self.kill_error)

    async def aclose(self) -> None:
        self.calls.append("agent.close")
        self.logs.close()
        self.lifecycle.close()
        self.game_events.close()


class ControllerStub:
    def __init__(self, calls: list[str]) -> None:
        self.calls = calls
        self.registered = asyncio.Event()
        self.unregistered = asyncio.Event()
        self.failure = asyncio.Event()
        self.logs = Broadcast[LogRecord]()
        self.lifecycle = Broadcast[LifecycleRecord]()
        self.events = Broadcast[GameEventRecord]()

    def subscribe_logs(self) -> Subscription[LogRecord]:
        return self.logs.subscribe()

    def subscribe_lifecycle(self) -> Subscription[LifecycleRecord]:
        return self.lifecycle.subscribe()

    def subscribe_events(self) -> Subscription[GameEventRecord]:
        return self.events.subscribe()

    async def register(self, endpoint: Any) -> None:
        self.endpoint = endpoint
        self.registered.set()

    async def unregister(self, _endpoint: Any) -> bool:
        self.unregistered.set()
        return True

    async def failed(self, endpoint: Any) -> bool:
        self.failed_agent = endpoint
        self.failure.set()
        return True

    async def invoke[T](self, command: c.Request[T]) -> T:
        assert isinstance(command, c.ClusterStatusQuery)
        status = ClusterStatus(
            epoch=ULID(),
            phase="waitingAgents",
            master="Master",
            missing_shards=("Master",),
            shards=(
                ShardRuntimeStatus(
                    name="Master",
                    is_master=True,
                    desired=ShardDesired.RUNNING,
                    phase=ShardPhase.UNAVAILABLE,
                    agent_incarnation=ULID(),
                    telemetry_profile="critical",
                ),
            ),
        )
        return cast(T, status)

    async def aclose(self) -> None:
        self.calls.append("controller.close")
        self.logs.close()
        self.lifecycle.close()
        self.events.close()


def write_deployment(root: Path) -> None:
    (root / "cluster.ini").write_text(
        "[SHARD]\nshard_enabled = true\nmaster_ip = 127.0.0.1\ncluster_key = test-key\n"
    )
    (root / "cluster_token.txt").write_text("test-token\n")
    for name, master, server_port, steam_port in (
        ("Caves", False, 11000, 27017),
        ("Master", True, 10999, 27016),
    ):
        directory = root / name
        directory.mkdir()
        (directory / "server.ini").write_text(
            f"""[SHARD]
is_master = {str(master).lower()}

[STEAM]
master_server_port = {steam_port}

[NETWORK]
server_port = {server_port}
""",
            encoding="utf-8",
        )


async def test_preexisting_shutdown_skips_daemon_initialization(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    shutdown = asyncio.Event()
    shutdown.set()

    async def run(*_: object) -> None:  # ruff: ignore[unused-async]
        pytest.fail("pre-stopped daemon must not initialize")

    monkeypatch.setattr(daemon, "_run_agent", run)
    assert await daemon.serve(shard="Caves", shutdown=shutdown) == 0


async def test_secondary_agent_identity_comes_from_server_ini(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    write_deployment(tmp_path)
    observed: list[Shard] = []

    class Agent:
        def __init__(self, shard: Shard, **_: object) -> None:
            observed.append(shard)

    async def serve(_: object, shutdown: asyncio.Event) -> None:
        await asyncio.sleep(0)
        shutdown.set()

    monkeypatch.setattr(daemon, "ShardAgent", Agent)
    monkeypatch.setattr(daemon, "_serve_agent", serve)
    await daemon._run_agent(
        "Caves",
        tmp_path / "install",
        tmp_path,
        30000,
        None,
        asyncio.Event(),
    )

    assert observed == [Shard("Caves", False)]


async def test_agent_rejects_a_shard_path_before_creating_files(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def agent(*_: object, **__: object) -> object:
        pytest.fail("invalid shard must not create an Agent")

    monkeypatch.setattr(daemon, "ShardAgent", agent)
    with pytest.raises(ValueError, match="unsafe DST shard"):
        await daemon._run_agent(
            "../../outside",
            tmp_path / "install",
            tmp_path,
            None,
            None,
            asyncio.Event(),
        )


async def test_master_discovers_roster_and_registers_local_agent(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    write_deployment(tmp_path)
    observed: dict[str, object] = {}
    instance = object()
    local = AgentStub([], master=True)

    def controller(configuration: object, **_: object) -> object:
        observed["controller"] = configuration
        return instance

    def agent(shard: Shard, **values: object) -> AgentStub:
        observed["agent"] = (shard, values)
        return local

    async def serve(
        value: object,
        endpoint: object,
        shutdown: asyncio.Event,
        *,
        public_socket: Path,
    ) -> None:
        await asyncio.sleep(0)
        observed["serve"] = (value, endpoint, public_socket)
        shutdown.set()

    monkeypatch.setattr(daemon, "ClusterController", controller)
    monkeypatch.setattr(daemon, "ShardAgent", agent)
    monkeypatch.setattr(daemon, "_serve_master", serve)
    await daemon._run_master(
        tmp_path / "install",
        tmp_path,
        30000,
        None,
        asyncio.Event(),
    )

    store = observed["controller"]
    assert isinstance(store, ConfigurationStore)
    assert store.shards == (
        Shard("Master", True),
        Shard("Caves", False),
    )
    assert store.directory == tmp_path
    shard, values = observed["agent"]  # ty: ignore[not-iterable]
    assert shard == Shard("Master", True)
    assert values["external_port"] == 30000
    assert observed["serve"] == (
        instance,
        local,
        tmp_path / ".dst-server.sock",
    )


async def test_secondary_command_rejects_the_master_shard(tmp_path: Path) -> None:
    write_deployment(tmp_path)
    with pytest.raises(ValueError, match="master command"):
        await daemon._run_agent(
            "Master",
            tmp_path / "install",
            tmp_path,
            None,
            None,
            asyncio.Event(),
        )


async def test_master_socket_survives_without_a_master_game_process(
    tmp_path: Path,
) -> None:
    tmp_path.chmod(0o700)  # ruff: ignore[blocking-path-method-in-async-function]
    calls: list[str] = []
    controller = ControllerStub(calls)
    agent = AgentStub(calls, master=True)
    shutdown = asyncio.Event()
    socket_path = tmp_path / "cluster.sock"
    address = f"dst-server-test-{ULID()}"

    async with rpc_runtime():
        task = asyncio.create_task(
            daemon._serve_master(
                controller,  # ty: ignore[invalid-argument-type]
                agent,  # ty: ignore[invalid-argument-type]
                shutdown,
                internal_address=address,
                public_socket=socket_path,
            )
        )
        while not socket_path.exists():  # ruff: ignore[async-busy-wait]
            await asyncio.sleep(0)
        async with await ClusterClient.connect(socket_path) as client:
            status = await client.invoke(c.ClusterStatusQuery())
            assert status.phase == "waitingAgents"
            shutdown.set()
            await task

    assert calls == ["controller.close", "agent.close"]
    assert not socket_path.exists()


async def test_preexisting_shutdown_closes_master_controller_and_agent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    shutdown = asyncio.Event()
    shutdown.set()
    calls: list[str] = []
    controller = ControllerStub(calls)
    agent = AgentStub(calls, master=True)

    @asynccontextmanager
    async def listener(*_: object) -> AsyncIterator[None]:
        pytest.fail("pre-stopped controller must not bind RPC listeners")
        yield

    monkeypatch.setattr(daemon, "abstract_rpc_server", listener)
    monkeypatch.setattr(daemon, "filesystem_rpc_server", listener)
    await daemon._serve_master(
        controller,  # ty: ignore[invalid-argument-type]
        agent,  # ty: ignore[invalid-argument-type]
        shutdown,
    )
    assert calls == ["controller.close", "agent.close"]

    secondary = AgentStub(calls)
    await daemon._serve_agent(secondary, shutdown)  # ty: ignore[invalid-argument-type]
    assert calls[-1] == "agent.close"


async def test_master_shutdown_wins_a_simultaneous_fatal_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    shutdown = asyncio.Event()
    calls: list[str] = []
    controller = ControllerStub(calls)

    class SimultaneousAgent(AgentStub):
        async def wait_fatal(self) -> None:
            shutdown.set()
            await asyncio.sleep(0)
            msg = "failure during shutdown"
            raise RuntimeError(msg)

    agent = SimultaneousAgent(calls, master=True)

    @asynccontextmanager
    async def listener(*_: object) -> AsyncIterator[None]:
        yield

    monkeypatch.setattr(daemon, "abstract_rpc_server", listener)
    monkeypatch.setattr(daemon, "filesystem_rpc_server", listener)

    await daemon._serve_master(
        controller,  # ty: ignore[invalid-argument-type]
        agent,  # ty: ignore[invalid-argument-type]
        shutdown,
    )

    assert calls == ["controller.close", "agent.close"]


@pytest.mark.parametrize("blocked", ["controller", "public_listener", "agent"])
@pytest.mark.parametrize("cleanup_error", [False, True])
async def test_master_repeated_cancellation_waits_for_all_resource_cleanup(
    monkeypatch: pytest.MonkeyPatch,
    blocked: str,
    cleanup_error: bool,
) -> None:
    existing_tasks = asyncio.all_tasks()
    calls: list[str] = []
    entered, release = asyncio.Event(), asyncio.Event()

    async def close(name: str) -> None:
        calls.append(name)
        if name == blocked:
            entered.set()
            await release.wait()
            if cleanup_error:
                raise RuntimeError(name)

    class Controller(ControllerStub):
        async def aclose(self) -> None:
            await close("controller")

    class Agent(AgentStub):
        async def aclose(self) -> None:
            await close("agent")

    @asynccontextmanager
    async def registry_listener(*_: object) -> AsyncIterator[None]:
        try:
            yield
        finally:
            await close("registry_listener")

    @asynccontextmanager
    async def public_listener(*_: object) -> AsyncIterator[None]:
        try:
            yield
        finally:
            await close("public_listener")

    monkeypatch.setattr(daemon, "abstract_rpc_server", registry_listener)
    monkeypatch.setattr(daemon, "filesystem_rpc_server", public_listener)
    controller = Controller(calls)
    agent = Agent(calls, master=True)
    serving = asyncio.create_task(
        daemon._serve_master(
            controller,  # ty: ignore[invalid-argument-type]
            agent,  # ty: ignore[invalid-argument-type]
            asyncio.Event(),
        )
    )
    try:
        async with asyncio.timeout(1):
            await agent.failure_waiting.wait()
            serving.cancel()
            await entered.wait()
            for _ in range(3):
                serving.cancel()
                await asyncio.sleep(0)
                assert not serving.done()
            release.set()
            with pytest.raises(asyncio.CancelledError):
                await serving
    finally:
        release.set()
        serving.cancel()
        await asyncio.gather(serving, return_exceptions=True)

    assert calls == ["controller", "public_listener", "registry_listener", "agent"]
    assert asyncio.all_tasks() <= existing_tasks


async def test_abstract_registry_registers_and_unregisters_remote_agent() -> None:
    calls: list[str] = []
    agent = AgentStub(calls)
    controller = ControllerStub(calls)
    address = f"dst-server-test-{ULID()}"
    task: asyncio.Task[None] | None = None
    async with asyncio.timeout(5), rpc_runtime():
        try:
            async with abstract_rpc_server(
                lambda: WorkerRegistryServant(controller),  # ty: ignore[invalid-argument-type]
                address,
            ):
                task = asyncio.create_task(daemon._registered_cycle(agent, address))  # ty: ignore[invalid-argument-type]
                await wait_for_event(controller.registered, task)
                await wait_for_event(agent.failure_waiting, task)
                assert controller.endpoint.name == "Caves"
            await task
            await wait_for_event(controller.unregistered)
        finally:
            async with asyncio.timeout(5):
                if task is not None:
                    task.cancel()
                    await asyncio.gather(task, return_exceptions=True)


@pytest.mark.parametrize("phase", ["connect", "register", "failed"])
async def test_registry_requests_time_out_and_release_connection(
    phase: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def blocked(*args: object, **kwargs: object) -> None:
        del args, kwargs
        await asyncio.Event().wait()

    agent = AgentStub([])
    servant = SimpleNamespace(aclose=AsyncMock())
    registry = SimpleNamespace(
        register=AsyncMock(return_value=SimpleNamespace(result=None)),
        failed=AsyncMock(return_value=SimpleNamespace(result=None)),
    )
    stream = Mock()
    client = Mock()
    client.bootstrap.return_value.cast_as.return_value = registry
    client.on_disconnect.return_value = asyncio.get_running_loop().create_future()
    connect = AsyncMock(return_value=stream)
    operation = connect if phase == "connect" else getattr(registry, phase)
    operation.side_effect = blocked
    monkeypatch.setattr(
        daemon,
        "capnp",
        SimpleNamespace(
            AsyncIoStream=SimpleNamespace(create_unix_connection=connect),
            TwoPartyClient=Mock(return_value=client),
        ),
    )
    monkeypatch.setattr(daemon, "AgentServant", Mock(return_value=servant))
    monkeypatch.setattr(daemon, "unwrap_outcome", Mock())
    monkeypatch.setattr(daemon, "DEFAULT_CONNECT_TIMEOUT", 0.01)
    monkeypatch.setattr(daemon, "DEFAULT_LIFECYCLE_TIMEOUT", 0.01)
    monkeypatch.setattr(daemon, "REGISTRY_FAILURE_TIMEOUT", 0.01)
    if phase == "failed":
        agent.failures.put_nowait(object())
    watchdog = asyncio.timeout(1)

    with pytest.raises(TimeoutError):
        async with watchdog:
            await daemon._registered_cycle(agent, "registry")  # ty: ignore[invalid-argument-type]

    assert not watchdog.expired()
    operation.assert_awaited_once()
    servant.aclose.assert_awaited_once()
    if phase == "connect":
        client.close.assert_not_called()
        stream.close.assert_not_called()
    else:
        client.close.assert_called_once()
        stream.close.assert_called_once()


@pytest.mark.parametrize("failure", ["servant", "client", "stream", "construct"])
async def test_registry_cleanup_failure_releases_remaining_transport_resources(
    monkeypatch: pytest.MonkeyPatch,
    failure: str,
) -> None:
    existing_tasks = asyncio.all_tasks()
    agent = AgentStub([])
    servant = Mock(aclose=AsyncMock())
    registry = Mock(register=AsyncMock(return_value=Mock(result=None)))
    stream, client = Mock(), Mock()
    client.bootstrap.return_value.cast_as.return_value = registry
    disconnected = asyncio.get_running_loop().create_future()
    disconnected.set_result(None)
    client.on_disconnect.return_value = disconnected
    construct = Mock(return_value=client)
    operation = {
        "servant": servant.aclose,
        "client": client.close,
        "stream": stream.close,
        "construct": construct,
    }[failure]
    operation.side_effect = RuntimeError(failure)
    connect = AsyncMock(return_value=stream)
    monkeypatch.setattr(
        daemon,
        "capnp",
        Mock(
            AsyncIoStream=Mock(create_unix_connection=connect),
            TwoPartyClient=construct,
        ),
    )
    monkeypatch.setattr(daemon, "AgentServant", Mock(return_value=servant))
    monkeypatch.setattr(daemon, "unwrap_outcome", Mock())

    with pytest.raises(RuntimeError, match=failure):
        await daemon._registered_cycle(agent, "registry")  # ty: ignore[invalid-argument-type]

    connect.assert_awaited_once()
    servant.aclose.assert_awaited_once()
    stream.close.assert_called_once()
    if failure == "construct":
        client.close.assert_not_called()
    else:
        client.close.assert_called_once()
    assert asyncio.all_tasks() <= existing_tasks


async def test_local_agent_failure_is_reported_without_rpc() -> None:
    controller = ControllerStub([])
    agent = AgentStub([], master=True)
    task = asyncio.create_task(
        daemon._report_local_failures(
            controller,  # ty: ignore[invalid-argument-type]
            agent,  # ty: ignore[invalid-argument-type]
        )
    )
    try:
        async with asyncio.timeout(5):
            await wait_for_event(agent.failure_waiting, task)
            agent.failures.put_nowait(object())
            await wait_for_event(controller.failure, task)
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

            assert controller.failed_agent is agent
    finally:
        async with asyncio.timeout(5):
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)


@pytest.mark.parametrize(
    ("kill_error", "expected_connections"),
    [(None, 2), ("child survived", 1)],
)
async def test_registry_exit_kills_before_reconnect(
    monkeypatch: pytest.MonkeyPatch,
    kill_error: str | None,
    expected_connections: int,
) -> None:
    calls: list[str] = []
    agent = AgentStub(calls, kill_error=kill_error)
    shutdown = asyncio.Event()
    connected = 0
    never = asyncio.Event()

    async def cycle(_: object, __: str) -> None:
        nonlocal connected
        connected += 1
        if connected == 1:
            return
        shutdown.set()
        await never.wait()

    monkeypatch.setattr(daemon, "_registered_cycle", cycle)
    outcome = (
        pytest.raises(RuntimeError, match=kill_error) if kill_error else nullcontext()
    )
    with outcome:
        await daemon._serve_agent(
            agent,  # ty: ignore[invalid-argument-type]
            shutdown,
            reconnect_delay=0,
        )

    assert connected == expected_connections
    assert calls == ["agent.kill", "agent.stop", "agent.close"]


def test_signal_handlers_set_shutdown_and_are_removed() -> None:
    shutdown = asyncio.Event()
    callbacks: dict[object, Any] = {}
    removed: list[object] = []

    class Loop:
        def add_signal_handler(self, number: object, callback: Any) -> None:
            callbacks[number] = callback

        def remove_signal_handler(self, number: object) -> bool:
            removed.append(number)
            return True

    remove = daemon._install_signal_handlers(
        shutdown,
        Loop(),  # ty: ignore[invalid-argument-type]
    )
    callbacks[next(iter(callbacks))]()
    assert shutdown.is_set()
    remove()
    assert removed == list(callbacks)
