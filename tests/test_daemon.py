import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager, nullcontext
from pathlib import Path
from typing import Any

import pytest
from ulid import ULID

from dst_server.cluster import daemon
from dst_server.cluster.configuration import ConfigurationStore
from dst_server.cluster.layout import Shard
from dst_server.cluster.subscriptions import Broadcast
from dst_server.rpc import ClusterClient, rpc_runtime
from dst_server.rpc.models import (
    ClusterStatus,
    GameEventRecord,
    LifecycleRecord,
    LogRecord,
    ShardRuntimeStatus,
)
from dst_server.rpc.servants import WorkerRegistryServant
from dst_server.rpc.transport import abstract_rpc_server


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
            desired="running",
            phase="stopped",
            agent_incarnation=ULID.from_str(self.incarnation),
            telemetry_profile="critical",
        )

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

    async def status(self) -> ClusterStatus:
        return ClusterStatus(
            epoch=ULID(),
            phase="waitingAgents",
            master="Master",
            missing_shards=("Master",),
            shards=(
                ShardRuntimeStatus(
                    name="Master",
                    is_master=True,
                    desired="running",
                    phase="unavailable",
                    agent_incarnation=ULID(),
                    telemetry_profile="critical",
                ),
            ),
        )

    async def aclose(self) -> None:
        self.calls.append("controller.close")


def write_deployment(root: Path) -> None:
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

    assert observed == [Shard("Caves", False, tmp_path / "Caves" / "console")]


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
        Shard("Master", True, tmp_path / "console"),
        Shard("Caves", False, tmp_path / "Caves" / "console"),
    )
    assert store.directory == tmp_path
    shard, values = observed["agent"]  # ty: ignore[not-iterable]
    assert shard == Shard("Master", True, tmp_path / "console")
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
            assert (await client.status()).phase == "waitingAgents"
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


async def test_abstract_registry_registers_and_unregisters_remote_agent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from dst_server.rpc import servants

    calls: list[str] = []
    agent = AgentStub(calls)
    controller = ControllerStub(calls)
    address = f"dst-server-test-{ULID()}"
    monkeypatch.setattr(servants, "ShardAgent", AgentStub)

    async with rpc_runtime():
        async with abstract_rpc_server(
            lambda: WorkerRegistryServant(controller),  # ty: ignore[invalid-argument-type]
            address,
        ):
            task = asyncio.create_task(daemon._registered_cycle(agent, address))  # ty: ignore[invalid-argument-type]
            await controller.registered.wait()
            await agent.failure_waiting.wait()
            assert controller.endpoint.name == "Caves"
        await task
        await controller.unregistered.wait()


async def test_local_agent_failure_is_reported_without_rpc() -> None:
    controller = ControllerStub([])
    agent = AgentStub([], master=True)
    task = asyncio.create_task(
        daemon._report_local_failures(
            controller,  # ty: ignore[invalid-argument-type]
            agent,  # ty: ignore[invalid-argument-type]
        )
    )
    await agent.failure_waiting.wait()
    agent.failures.put_nowait(object())
    await controller.failure.wait()
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)

    assert controller.failed_agent is agent


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
