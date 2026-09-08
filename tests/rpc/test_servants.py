# ruff: file-ignore[blocking-path-method-in-async-function]
import asyncio
import gc
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager, suppress
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock
from weakref import ref

import pytest
from pydantic import ValidationError
from ulid import ULID

from dst_server import commands as c
from dst_server.api import ClusterAPI, ShardAPI
from dst_server.cluster.subscriptions import Broadcast, Subscription
from dst_server.errors import (
    ErrorCode,
    IndeterminateCommandError,
    IndeterminateError,
    RemoteError,
)
from dst_server.events.server import SavedEvent
from dst_server.models.cluster import (
    ClusterStatus,
    LogRecord,
    ShardDesired,
    ShardPhase,
    ShardRuntimeStatus,
)
from dst_server.models.snapshot import (
    Snapshot,
    SnapshotCatalog,
    SnapshotClock,
    WorldSnapshotMetadata,
)
from dst_server.rpc import servants as servant_module
from dst_server.rpc.client import ClusterClient, rpc_runtime
from dst_server.rpc.codec import ERROR, decode, unwrap_outcome
from dst_server.rpc.schema import SCHEMA_FINGERPRINT, load_schema
from dst_server.rpc.servants import (
    AgentServant,
    BootstrapServant,
    RemoteAgent,
    WorkerRegistryServant,
)
from dst_server.rpc.transport import abstract_rpc_server, filesystem_rpc_server
from tests.cluster.test_controller import controller as make_controller

capnp: Any = pytest.importorskip("capnp")
type Hook = Callable[[c.Request[Any]], Awaitable[Any]]


def shard_status() -> ShardRuntimeStatus:
    return ShardRuntimeStatus(
        name="Master",
        is_master=True,
        desired=ShardDesired.RUNNING,
        phase=ShardPhase.RUNNING,
        agent_incarnation=ULID(),
        ready=True,
        telemetry_profile="critical",
    )


def log_record(sequence: int, line: str = "line") -> LogRecord:
    return LogRecord(
        shard="Master",
        game_attempt=ULID(),
        sequence=sequence,
        observed_timestamp_ns=sequence,
        line=line,
    )


class FakeShard(ShardAPI):
    def __init__(self) -> None:
        self.value = shard_status()
        self.logs = Broadcast[LogRecord]()
        self.lifecycle = Broadcast[Any]()
        self.game_events = Broadcast[Any]()
        self.requests: list[c.Request[Any]] = []
        self.hook: Hook | None = None
        self.catalog = SnapshotCatalog(
            session_id="SESSION",
            snapshots=(
                Snapshot(
                    snapshot_id=31,
                    world_file="session/SESSION/0000000031",
                    metadata=WorldSnapshotMetadata(clock=SnapshotClock(cycles=20)),
                ),
                Snapshot(snapshot_id=0),
            ),
            has_more=True,
        )

    async def invoke[T](self, command: c.Request[T]) -> T:
        self.requests.append(command)
        if self.hook is not None:
            return cast("T", await self.hook(command))
        match command:
            case c.Status():
                value = self.value
            case c.Execute():
                value = command.source
            case c.Snapshots():
                value = self.catalog
            case c.Save():
                value = SavedEvent(path="session/SESSION/0000000031", snapshot=31)
            case _:
                value = None
        return cast("T", value)

    def subscribe_logs(self) -> Subscription[LogRecord]:
        return self.logs.subscribe()

    def subscribe_lifecycle(self) -> Subscription[Any]:
        return self.lifecycle.subscribe()

    def subscribe_events(self) -> Subscription[Any]:
        return self.game_events.subscribe()


class FakeController(ClusterAPI):
    def __init__(self) -> None:
        self.master = FakeShard()
        self.value = ClusterStatus(
            epoch=ULID(), phase="running", master="Master", shards=(self.master.value,)
        )
        self.requests: list[c.Request[Any]] = []
        self.hook: Hook | None = None

    async def invoke[T](self, command: c.Request[T]) -> T:
        self.requests.append(command)
        if self.hook is not None:
            return cast("T", await self.hook(command))
        match command:
            case c.ClusterStatusQuery():
                value = self.value
            case c.Snapshots():
                value = self.master.catalog
            case c.RollbackToDay():
                value = self.master.catalog.snapshots[0]
            case _:
                value = None
        return cast("T", value)

    def shard(self, name: str) -> FakeShard:
        if name != "Master":
            raise KeyError(name)
        return self.master

    def subscribe_logs(self) -> Subscription[LogRecord]:
        return self.master.logs.subscribe()

    def subscribe_lifecycle(self) -> Subscription[Any]:
        return self.master.lifecycle.subscribe()

    def subscribe_events(self) -> Subscription[Any]:
        return self.master.game_events.subscribe()


@asynccontextmanager
async def connected(
    tmp_path: Path, controller: FakeController
) -> AsyncIterator[ClusterClient]:
    tmp_path.chmod(0o700)
    path = tmp_path / "cluster.sock"
    async with (
        rpc_runtime(),
        filesystem_rpc_server(path, lambda: BootstrapServant(controller)),
        await ClusterClient.connect(path) as client,
    ):
        yield client


async def test_typed_commands_cross_real_capabilities(tmp_path: Path) -> None:
    controller = FakeController()
    async with connected(tmp_path, controller) as client:
        assert await client.status() == controller.value
        shard = client.shard("Master")
        assert shard is client.shard("Master")
        assert await shard.status() == controller.master.value
        assert await shard.execute("return 1", timeout=4) == "return 1"
        assert await shard.save(timeout=5) == SavedEvent(
            path="session/SESSION/0000000031", snapshot=31
        )
        assert (
            await client.list_snapshots(limit=17, before=0) == controller.master.catalog
        )
        assert (
            await client.rollback_to_day(21, timeout=6)
            == controller.master.catalog.snapshots[0]
        )
        assert controller.master.requests[-2:] == [
            c.Execute(source="return 1", timeout=4),
            c.Save(timeout=5),
        ]
        assert controller.requests[-2:] == [
            c.Snapshots(limit=17, before=0),
            c.RollbackToDay(day=21, timeout=6),
        ]


@pytest.mark.parametrize(
    "payload",
    [
        b'{"method":"start","method":"stop"}',
        b'{"method":"start","arguments":{"timeout":1}}',
        b'{"method":"start","timeout":0}',
        b'{"method":"start","timeout":NaN}',
        b'{"method":"start","timeout":true}',
        b'{"method":"wait_saved","arguments":{}}',
        b'{"method":"__getattribute__"}',
        b'{"method":"status","unknown":true}',
    ],
)
async def test_untrusted_requests_fail_before_dispatch(
    tmp_path: Path, payload: bytes
) -> None:
    controller = FakeController()
    async with connected(tmp_path, controller) as client:
        response = await client._capability.call(request=payload)
        with pytest.raises(RemoteError) as failure:
            unwrap_outcome(response.result)
        assert failure.value.error.code is ErrorCode.INVALID_ARGUMENT
        assert controller.requests == []


@pytest.mark.parametrize(
    "command",
    [
        c.Save().model_copy(update={"timeout": 0}),
        c.Snapshots().model_copy(update={"before": True}),
    ],
)
async def test_invalid_copied_command_fails_before_opening_capability(
    command: c.Request[Any],
) -> None:
    client = ClusterClient(None, None, None)
    with pytest.raises(ValidationError):
        await client.shard("Master").invoke(command)


async def test_unknown_shard_and_fingerprint_are_reported(tmp_path: Path) -> None:
    controller = FakeController()
    async with connected(tmp_path, controller) as client:
        with pytest.raises(RemoteError) as unknown:
            await client.shard("Unknown").status()
        assert unknown.value.error.code is ErrorCode.NOT_FOUND
        bootstrap = client._client.bootstrap().cast_as(load_schema().Bootstrap)
        response = await bootstrap.connect(schemaFingerprint="0" * 64)
        with pytest.raises(RemoteError) as incompatible:
            unwrap_outcome(response.result)
        assert incompatible.value.error.code is ErrorCode.INCOMPATIBLE_SCHEMA


@pytest.mark.parametrize(
    ("mutation", "expected"),
    [(False, ErrorCode.TIMEOUT), (True, ErrorCode.INDETERMINATE)],
)
async def test_server_deadlines_distinguish_queries_and_mutations(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mutation: bool, expected: ErrorCode
) -> None:
    monkeypatch.setattr(servant_module, "RPC_TIMEOUT_MARGIN", 0.01)
    controller = FakeController()

    async def slow(_: c.Request[Any]) -> None:
        await asyncio.sleep(1)

    controller.hook = slow
    async with connected(tmp_path, controller) as client:
        command = (
            c.Start(timeout=0.02) if mutation else c.ClusterStatusQuery(timeout=0.02)
        )
        with pytest.raises(RemoteError) as failure:
            await client.invoke(command)
        assert failure.value.error.code is expected


@pytest.mark.parametrize("command_type", [c.ClusterSave, c.Reset])
async def test_workflow_budget_starts_after_controller_lock(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    command_type: type[c.ClusterSave | c.Reset],
) -> None:
    controller, master, caves, _, _ = await make_controller(tmp_path, monkeypatch)
    received = asyncio.Event()
    elapsed = asyncio.Event()
    invoke = controller.invoke

    async def observed[T](command: c.Request[T]) -> T:
        received.set()
        return await invoke(command)

    monkeypatch.setattr(controller, "invoke", observed)
    await controller._lock.acquire()
    pending: asyncio.Task[Any] | None = None
    try:
        tmp_path.chmod(0o700)
        path = tmp_path / "cluster.sock"
        async with (
            rpc_runtime(),
            filesystem_rpc_server(path, lambda: BootstrapServant(controller)),
            await ClusterClient.connect(path) as client,
        ):
            pending = asyncio.create_task(client.invoke(command_type(timeout=0.1)))
            await received.wait()
            timer = asyncio.get_running_loop().call_later(0.15, elapsed.set)
            try:
                await elapsed.wait()
            finally:
                timer.cancel()
            assert not pending.done()
            controller._lock.release()
            result = await pending
            if command_type is c.ClusterSave:
                assert result.snapshot == 7
                assert tuple(name for name, _ in result.shards) == ("Master", "Caves")
                assert c.Save(timeout=0.1) in master.requests
                assert (
                    c.WaitSaved(cursor=caves.save_cursor, snapshot=7, timeout=0.1)
                    in caves.requests
                )
            else:
                assert result is None
                assert c.Reset(timeout=0.1) in master.requests
                assert (
                    c.WaitGeneration(cursor=caves.generation_cursor, timeout=0.1)
                    in caves.requests
                )
    finally:
        if controller._lock.locked():
            controller._lock.release()
        if pending is not None:
            with suppress(BaseException):
                await pending
        await controller.aclose()


@pytest.mark.parametrize(
    ("error", "expected"),
    [
        (ValueError("secret"), ErrorCode.INVALID_ARGUMENT),
        (RuntimeError("secret"), ErrorCode.INVALID_STATE),
        (KeyError("secret"), ErrorCode.NOT_FOUND),
        (OSError("secret"), ErrorCode.INTERNAL),
        (IndeterminateCommandError("secret"), ErrorCode.INDETERMINATE),
    ],
)
async def test_remote_errors_are_typed_and_do_not_expose_exception_messages(
    tmp_path: Path, error: Exception, expected: ErrorCode
) -> None:
    controller = FakeController()
    controller.hook = AsyncMock(side_effect=error)
    async with connected(tmp_path, controller) as client:
        with pytest.raises(RemoteError) as failure:
            await client.status()
        assert failure.value.error.code is expected
        assert "secret" not in str(failure.value)


@pytest.mark.parametrize("mutation", [False, True])
async def test_invalid_result_preserves_uncertainty(
    tmp_path: Path, mutation: bool
) -> None:
    controller = FakeController()
    controller.hook = AsyncMock(return_value=object())
    async with connected(tmp_path, controller) as client:
        command = c.Start() if mutation else c.ClusterStatusQuery()
        with pytest.raises(RemoteError) as failure:
            await client.invoke(command)
        assert failure.value.error.code is (
            ErrorCode.INDETERMINATE if mutation else ErrorCode.INTERNAL
        )


@pytest.mark.parametrize("disconnect", [False, True])
async def test_accepted_mutation_survives_caller_loss(
    tmp_path: Path, disconnect: bool
) -> None:
    entered, release, completed = asyncio.Event(), asyncio.Event(), asyncio.Event()

    async def mutate(_: c.Request[Any]) -> None:
        entered.set()
        await release.wait()
        completed.set()

    controller = FakeController()
    controller.hook = mutate
    async with connected(tmp_path, controller) as client:
        pending = asyncio.create_task(client.start())
        await entered.wait()
        if disconnect:
            client.close()
        else:
            pending.cancel()
        with pytest.raises(IndeterminateError):
            await pending
        release.set()
        async with asyncio.timeout(1):
            await completed.wait()


async def test_query_cancellation_reaches_handler(tmp_path: Path) -> None:
    entered, cancelled = asyncio.Event(), asyncio.Event()

    async def query(_: c.Request[Any]) -> None:
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    controller = FakeController()
    controller.hook = query
    async with connected(tmp_path, controller) as client:
        pending = asyncio.create_task(client.status())
        await entered.wait()
        pending.cancel()
        with pytest.raises(asyncio.CancelledError):
            await pending
        async with asyncio.timeout(1):
            await cancelled.wait()


async def test_subscription_overflow_is_recoverable_and_close_releases(
    tmp_path: Path,
) -> None:
    controller = FakeController()
    async with connected(tmp_path, controller) as client:
        subscription = await client.subscribe_logs()
        for sequence in range(1025):
            controller.master.logs.publish(log_record(sequence))
        with pytest.raises(RemoteError) as overflow:
            await subscription.next()
        assert overflow.value.error.code is ErrorCode.OVERFLOW
        assert (await subscription.next())[0].sequence == 1024
        await subscription.close()
        assert subscription.closed
        assert not controller.master.logs._subscriptions


async def test_subscription_validation_and_capability_gc(tmp_path: Path) -> None:
    controller = FakeController()
    async with connected(tmp_path, controller) as client:
        subscription = await client.subscribe_logs()
        with pytest.raises(ValidationError):
            await subscription.next(0)
        response = await subscription._capability.next(maxItems=0)
        assert decode(ERROR, response.batch.error).code is ErrorCode.INVALID_ARGUMENT
        assert not controller.master.logs._subscriptions
        subscription = await client.subscribe_logs()
        del subscription
        gc.collect()
        async with asyncio.timeout(1):
            while controller.master.logs._subscriptions:  # ruff: ignore[async-busy-wait]
                await asyncio.sleep(0)


async def test_repeated_connections_release_subscriptions_and_roots(
    tmp_path: Path,
) -> None:
    tmp_path.chmod(0o700)
    controller = FakeController()
    roots = []

    def bootstrap() -> BootstrapServant:
        servant = BootstrapServant(controller)
        roots.append(ref(servant))
        return servant

    path = tmp_path / "cluster.sock"
    async with rpc_runtime(), filesystem_rpc_server(path, bootstrap) as server:
        for sequence in range(10):
            async with (
                await ClusterClient.connect(path) as client,
                await client.subscribe_logs() as subscription,
            ):
                controller.master.logs.publish(log_record(sequence))
                assert (await subscription.next())[0].sequence == sequence
            del subscription, client
        async with asyncio.timeout(1):
            while server.tasks:  # ruff: ignore[async-busy-wait]
                await asyncio.sleep(0)
        assert not server.connections
        assert not controller.master.logs._subscriptions
        assert all(reference() is None for reference in roots)


async def test_shard_handles_are_cached_only_while_in_use(tmp_path: Path) -> None:
    controller = FakeController()
    async with connected(tmp_path, controller) as client:
        shard = client.shard("Master")
        assert shard is client.shard("Master")
        references = [ref(client.shard(f"Missing-{index}")) for index in range(100)]
        assert all(reference() is None for reference in references)
        assert len(client._shards) == 1


def test_client_close_releases_connection_and_shard_capabilities() -> None:
    class Resource:
        def close(self) -> None:
            pass

    resources = [Resource() for _ in range(4)]
    references = tuple(ref(resource) for resource in resources)
    client = ClusterClient(*resources[:3])
    shard = client.shard("Master")
    shard._capability = resources[3]
    del resources

    client.close()

    assert all(reference() is None for reference in references)


async def test_remote_relay_releases_delivered_batch() -> None:
    agent = RemoteAgent(None)
    source, target = Broadcast[LogRecord](), Broadcast[LogRecord]()
    incoming, outgoing = source.subscribe(), target.subscribe()
    subscription: Any = SimpleNamespace(next=lambda: incoming.next(256))
    relay = asyncio.create_task(agent._relay_stream(subscription, target, LogRecord))
    references = []
    for sequence in range(3):
        record = log_record(sequence, "x" * 1024 * 1024)
        references.append(ref(record))
        source.publish(record)
    del record
    try:
        async with asyncio.timeout(1):
            batch = await outgoing.next(3)
        assert len(batch) == 3
        del batch
        assert all(reference() is None for reference in references)
    finally:
        source.close()
        await relay
        outgoing.close()
        await agent.aclose()


async def test_remote_reconnect_does_not_retain_failed_stream_frames(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    agent = RemoteAgent(None)
    reconnecting = asyncio.Event()
    references = []

    class BrokenSubscription:
        async def next(self) -> None:
            record = log_record(0, "x" * 1024 * 1024)
            references.append(ref(record))
            msg = "stream failed"
            raise ValueError(msg)

        async def close(self) -> None:
            pass

    async def subscribe(*_: object) -> None:
        reconnecting.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(servant_module, "_STREAM_RETRY_DELAY", 0)
    monkeypatch.setattr(agent, "_subscribe", subscribe)
    pump = asyncio.create_task(
        agent._pump(cast("Any", BrokenSubscription()), "logs", agent.logs, LogRecord)
    )
    try:
        async with asyncio.timeout(1):
            await reconnecting.wait()
        assert references
        assert references[0]() is None
    finally:
        pump.cancel()
        with suppress(asyncio.CancelledError):
            await pump
        await agent.aclose()


async def test_remote_agent_close_releases_capability() -> None:
    class Capability:
        pass

    capability = Capability()
    reference = ref(capability)
    agent = RemoteAgent(capability)
    del capability

    await agent.aclose()

    assert reference() is None


async def test_client_releases_encoded_request_while_waiting(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from dst_server.rpc import client as client_module

    class Payload(bytearray):
        pass

    sent = asyncio.Event()
    response = asyncio.get_running_loop().create_future()
    references = []

    def encode(_: c.Request[Any]) -> Payload:
        payload = Payload(1024 * 1024)
        references.append(ref(payload))
        return payload

    def send(*, request: Payload) -> asyncio.Future[Any]:
        assert len(request) == 1024 * 1024
        sent.set()
        return response

    monkeypatch.setattr(client_module, "encode_request", encode)
    client = ClusterClient(None, None, SimpleNamespace(call=send))
    pending = asyncio.create_task(client.status())
    try:
        await sent.wait()
        assert references
        assert references[0]() is None
    finally:
        pending.cancel()
        with suppress(asyncio.CancelledError):
            await pending


async def test_call_releases_native_request_before_running_handler(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    entered, release = asyncio.Event(), asyncio.Event()
    respond = servant_module._EndpointMethods._respond

    async def inspect_context(
        self: Any, context: Any, method: str, *args: Any, **kwargs: Any
    ) -> None:
        if method == "execute":
            with pytest.raises(capnp.KjException, match="releaseParams"):
                _ = context.params
            entered.set()
        await respond(self, context, method, *args, **kwargs)

    async def blocked(command: c.Request[Any]) -> str:
        await release.wait()
        assert isinstance(command, c.Execute)
        return command.source

    monkeypatch.setattr(servant_module._EndpointMethods, "_respond", inspect_context)
    controller = FakeController()
    controller.master.hook = blocked
    async with connected(tmp_path, controller) as client:
        pending = asyncio.create_task(client.shard("Master").execute("x" * 1024 * 1024))
        try:
            async with asyncio.timeout(1):
                await entered.wait()
        finally:
            release.set()
            await pending


async def test_connection_timeout_covers_socket_creation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def blocked(_: str) -> None:
        await asyncio.Event().wait()

    from dst_server.rpc import client as module

    monkeypatch.setattr(
        module,
        "capnp",
        SimpleNamespace(AsyncIoStream=SimpleNamespace(create_unix_connection=blocked)),
    )
    with pytest.raises(TimeoutError):
        await ClusterClient.connect("unused", timeout=0.01)


class RegistryController:
    def __init__(self, *, blocked: bool = False) -> None:
        self.registered: Any = None
        self.unregistered = asyncio.Event()
        self.registration_entered = asyncio.Event()
        self.registration_release = asyncio.Event()
        self.blocked = blocked
        self.registration_calls = 0

    async def register(self, endpoint: Any) -> None:
        self.registration_calls += 1
        self.registration_entered.set()
        if self.blocked:
            await self.registration_release.wait()
        self.registered = endpoint

    async def unregister(self, endpoint: Any) -> bool:
        assert endpoint is self.registered
        self.unregistered.set()
        return True

    async def failed(self, endpoint: Any) -> bool:
        return endpoint is self.registered


async def open_registry(name: str) -> tuple[Any, Any, Any, Any]:
    stream = await capnp.AsyncIoStream.create_unix_connection(f"\0{name}")
    client = capnp.TwoPartyClient(stream)
    return (
        stream,
        client,
        client.bootstrap().cast_as(load_schema().WorkerRegistry),
        client.on_disconnect(),
    )


async def test_registry_fingerprint_capability_and_disconnect_lifecycle() -> None:
    controller = RegistryController()
    target = FakeShard()
    servant = AgentServant(target)
    name = f"dst-registry-{ULID()}"
    async with (
        rpc_runtime(),
        abstract_rpc_server(lambda: WorkerRegistryServant(controller), name),
    ):
        stream, client, registry, disconnected = await open_registry(name)
        response = await registry.register(schemaFingerprint="0" * 64, agent=servant)
        with pytest.raises(RemoteError) as failure:
            unwrap_outcome(response.result)
        assert failure.value.error.code is ErrorCode.INCOMPATIBLE_SCHEMA
        unwrap_outcome(
            (
                await registry.register(
                    schemaFingerprint=SCHEMA_FINGERPRINT, agent=servant
                )
            ).result
        )
        remote = controller.registered
        assert (remote.name, remote.master, remote.incarnation) == (
            "Master",
            True,
            str(target.value.agent_incarnation),
        )
        assert await remote.invoke(c.Snapshots(limit=7, before=0)) == target.catalog
        assert target.requests[-1] == c.Snapshots(limit=7, before=0)
        with pytest.raises(RemoteError) as duplicate:
            unwrap_outcome(
                (
                    await registry.register(
                        schemaFingerprint=SCHEMA_FINGERPRINT, agent=servant
                    )
                ).result
            )
        assert duplicate.value.error.code is ErrorCode.INVALID_STATE
        forwarded = remote.logs.subscribe()
        record = log_record(1, "forwarded")
        target.logs.publish(record)
        async with asyncio.timeout(1):
            assert await forwarded.next(1) == (record,)
        forwarded.close()
        unwrap_outcome((await registry.failed()).result)
        client.close()
        stream.close()
        async with asyncio.timeout(1):
            await disconnected
            await controller.unregistered.wait()
    await servant.aclose()
    assert not target.logs._subscriptions


async def test_disconnect_during_registration_rolls_back_capability() -> None:
    controller = RegistryController(blocked=True)
    target = FakeShard()
    servant = AgentServant(target)
    name = f"dst-registry-race-{ULID()}"
    async with (
        rpc_runtime(),
        abstract_rpc_server(lambda: WorkerRegistryServant(controller), name),
    ):
        stream, client, registry, disconnected = await open_registry(name)
        pending = asyncio.ensure_future(
            registry.register(schemaFingerprint=SCHEMA_FINGERPRINT, agent=servant)
        )
        await controller.registration_entered.wait()
        client.close()
        stream.close()
        await disconnected
        controller.registration_release.set()
        with suppress(BaseException):
            await pending
        async with asyncio.timeout(1):
            await controller.unregistered.wait()
    await servant.aclose()
    assert not target.logs._subscriptions
