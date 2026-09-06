# ruff: file-ignore[blocking-path-method-in-async-function, missing-return-type-undocumented-public-function]
import asyncio
import gc
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager, suppress
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any

import pytest
from ulid import ULID

from dst_server.cluster.subscriptions import Broadcast
from dst_server.models.snapshot import (
    Snapshot,
    SnapshotCatalog,
    SnapshotClock,
    WorldSnapshotMetadata,
)
from dst_server.rpc.client import ClusterClient, rpc_runtime
from dst_server.rpc.errors import (
    DisconnectedError,
    ErrorCode,
    IndeterminateError,
    RemoteError,
    unwrap_outcome,
)
from dst_server.rpc.models import ClusterStatus, LogRecord, ShardRuntimeStatus
from dst_server.rpc.schema import SCHEMA_FINGERPRINT, load_schema
from dst_server.rpc.servants import (
    AgentServant,
    BootstrapServant,
    WorkerRegistryServant,
)
from dst_server.rpc.transport import abstract_rpc_server, filesystem_rpc_server
from dst_server.runtime import IndeterminateCommandError

capnp: Any = pytest.importorskip("capnp")


def shard_status(*, incarnation: str | None = None) -> ShardRuntimeStatus:
    return ShardRuntimeStatus(
        name="Master",
        is_master=True,
        desired="running",
        phase="running",
        agent_incarnation=(
            ULID.from_str(incarnation) if incarnation is not None else None
        ),
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


class FakeShard:
    def __init__(self, status: ShardRuntimeStatus) -> None:
        self.value = status
        self.logs = Broadcast[LogRecord]()
        self.lifecycle = Broadcast[Any]()
        self.game_events = Broadcast[Any]()
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
        self.snapshot_requests: list[tuple[int, int | None]] = []
        self.rollback_request: tuple[str, int, float] | None = None
        self.rollback_error: BaseException | None = None

    async def status(self) -> ShardRuntimeStatus:
        return self.value

    async def execute(self, source: str, completion_timeout: float) -> str:
        assert completion_timeout > 0
        return source

    async def list_snapshots(
        self, limit: int = 100, *, before: int | None = None
    ) -> SnapshotCatalog:
        self.snapshot_requests.append((limit, before))
        return self.catalog

    async def rollback_to_snapshot(
        self, session_id: str, snapshot_id: int, completion_timeout: float = 30
    ) -> None:
        self.rollback_request = (session_id, snapshot_id, completion_timeout)
        if self.rollback_error is not None:
            raise self.rollback_error

    def subscribe_logs(self):
        return self.logs.subscribe()

    def subscribe_lifecycle(self):
        return self.lifecycle.subscribe()

    def subscribe_events(self):
        return self.game_events.subscribe()


class FakeController:
    def __init__(self) -> None:
        status = shard_status()
        self.value = ClusterStatus(
            epoch=ULID(),
            phase="running",
            master="Master",
            shards=(status,),
        )
        self.master = FakeShard(status)
        self.status_error: BaseException | None = None
        self.start_error: BaseException | None = None
        self.started = asyncio.Event()
        self.release = asyncio.Event()
        self.completed = asyncio.Event()
        self.rollback_request: tuple[int, float] | None = None

    async def status(self) -> ClusterStatus:
        if self.status_error is not None:
            raise self.status_error
        return self.value

    def shard(self, name: str) -> FakeShard:
        if name != "Master":
            raise KeyError(name)
        return self.master

    async def start(self) -> None:
        self.started.set()
        if self.start_error is not None:
            raise self.start_error
        await self.release.wait()
        self.completed.set()

    async def list_snapshots(
        self, limit: int = 100, *, before: int | None = None
    ) -> SnapshotCatalog:
        return await self.master.list_snapshots(limit, before=before)

    async def rollback_to_day(
        self, day: int, completion_timeout: float = 30
    ) -> Snapshot:
        self.rollback_request = (day, completion_timeout)
        return self.master.catalog.snapshots[0]

    def subscribe_logs(self):
        return self.master.logs.subscribe()


@asynccontextmanager
async def connected(
    tmp_path: Path,
    controller: FakeController,
) -> AsyncIterator[ClusterClient]:
    tmp_path.chmod(0o700)
    path = tmp_path / "cluster.sock"
    async with (
        rpc_runtime(),
        filesystem_rpc_server(
            path,
            lambda: BootstrapServant(controller),  # ty: ignore[invalid-argument-type]
        ),
        await ClusterClient.connect(path) as client,
    ):
        yield client


async def test_public_servant_success_smoke(tmp_path: Path) -> None:
    controller = FakeController()
    async with connected(tmp_path, controller) as client:
        assert await client.status() == controller.value
        shard = client.shard("Master")
        assert shard is client.shard("Master")
        assert await shard.status() == controller.master.value
        assert await shard.execute("return 1", timeout=4) == "return 1"


@pytest.mark.parametrize("before", [None, 0, 2**63 + 1, 2**64 - 1])
async def test_snapshot_catalog_and_day_rollback_round_trip(
    before: int | None,
) -> None:
    controller = FakeController()
    with TemporaryDirectory(prefix="dst-rpc-snapshots-") as directory:
        async with connected(Path(directory), controller) as client:
            catalog = await client.list_snapshots(17, before=before)
            assert catalog == controller.master.catalog
            assert (
                await client.shard("Master").list_snapshots(17, before=before)
                == catalog
            )
            assert controller.master.snapshot_requests == [(17, before), (17, before)]

            snapshot = await client.rollback_to_day(21, timeout=4)
            assert snapshot == catalog.snapshots[0]
            assert snapshot.metadata is not None
            assert snapshot.metadata.day == 21
            assert controller.rollback_request == (21, 4.0)

            with pytest.raises(ValueError, match="timeout"):
                await client.rollback_to_day(21, timeout=0)


@pytest.mark.parametrize("value", [-1, True, 1.5, 2**64])
async def test_snapshot_clients_reject_invalid_uint64_before_sending(
    value: Any,
) -> None:
    controller = FakeController()
    with TemporaryDirectory(prefix="dst-rpc-snapshot-validation-") as directory:
        async with connected(Path(directory), controller) as client:
            with pytest.raises(ValueError, match=r"integer|UInt64"):
                await client.rollback_to_day(value)
            for endpoint in (client, client.shard("Master")):
                with pytest.raises(ValueError, match=r"integer|limit"):
                    await endpoint.list_snapshots(value)
                with pytest.raises(ValueError, match=r"integer|UInt64"):
                    await endpoint.list_snapshots(before=value)

            assert controller.rollback_request is None
            assert controller.master.snapshot_requests == []
            assert await client.status() == controller.value


async def test_snapshot_client_limits_and_timeouts_are_validated_before_sending() -> (
    None
):
    controller = FakeController()
    with TemporaryDirectory(prefix="dst-rpc-snapshot-boundaries-") as directory:
        async with connected(Path(directory), controller) as client:
            with pytest.raises(ValueError, match="positive"):
                await client.rollback_to_day(0)
            for timeout in (0, -1, True, float("inf"), float("nan")):
                with pytest.raises(ValueError, match="timeout"):
                    await client.rollback_to_day(21, timeout=timeout)
            for endpoint in (client, client.shard("Master")):
                for limit in (0, 101):
                    with pytest.raises(ValueError, match=r"positive|limit"):
                        await endpoint.list_snapshots(limit)
            assert controller.rollback_request is None
            assert controller.master.snapshot_requests == []

            await client.rollback_to_day(2**64 - 1)
            assert controller.rollback_request == (2**64 - 1, 30.0)


@pytest.mark.parametrize(
    ("error", "code"),
    [
        (ValueError("private"), ErrorCode.INVALID_ARGUMENT),
        (KeyError("private"), ErrorCode.NOT_FOUND),
        (RuntimeError("private"), ErrorCode.INVALID_STATE),
        (DisconnectedError("private"), ErrorCode.UNAVAILABLE),
        (TimeoutError(), ErrorCode.TIMEOUT),
        (Exception("NEVER-CROSS-RPC"), ErrorCode.INTERNAL),
    ],
    ids=lambda value: (
        value.value if isinstance(value, ErrorCode) else type(value).__name__
    ),
)
async def test_public_query_error_matrix(
    tmp_path: Path,
    error: BaseException,
    code: ErrorCode,
) -> None:
    controller = FakeController()
    controller.status_error = error

    async with connected(tmp_path, controller) as client:
        with pytest.raises(RemoteError) as failed:
            await client.status()

    assert failed.value.error.code is code
    assert "private" not in str(failed.value)
    assert "NEVER-CROSS-RPC" not in str(failed.value)


@pytest.mark.parametrize("error", [TimeoutError(), IndeterminateCommandError("lost")])
async def test_uncertain_mutation_errors_are_indeterminate(
    tmp_path: Path,
    error: BaseException,
) -> None:
    controller = FakeController()
    controller.start_error = error

    async with connected(tmp_path, controller) as client:
        with pytest.raises(IndeterminateError) as failed:
            await client.start()

    assert failed.value.error.code is ErrorCode.INDETERMINATE


@pytest.mark.parametrize("disconnect", [False, True], ids=["cancel", "disconnect"])
async def test_accepted_mutation_survives_caller_loss(
    tmp_path: Path,
    disconnect: bool,
) -> None:
    controller = FakeController()
    async with connected(tmp_path, controller) as client:
        pending = asyncio.create_task(client.start())
        await controller.started.wait()
        if disconnect:
            client.close()
        else:
            pending.cancel()
        with pytest.raises(IndeterminateError):
            await pending

        controller.release.set()
        async with asyncio.timeout(1):
            await controller.completed.wait()


async def test_public_subscription_overflow_is_recoverable(tmp_path: Path) -> None:
    controller = FakeController()
    async with connected(tmp_path, controller) as client:
        subscription = await client.subscribe_logs()
        for sequence in range(1025):
            controller.master.logs.publish(log_record(sequence))

        with pytest.raises(RemoteError) as overflow:
            await subscription.next(1)
        assert overflow.value.error.code is ErrorCode.OVERFLOW
        assert not subscription.closed

        controller.master.logs.publish(log_record(1025, "after-overflow"))
        assert (await subscription.next(256))[-1].line == "after-overflow"
        await subscription.close()

    assert not controller.master.logs._subscriptions


async def test_subscription_arguments_are_validated_at_the_server_boundary(
    tmp_path: Path,
) -> None:
    controller = FakeController()
    async with connected(tmp_path, controller) as client:
        subscription = await client.subscribe_logs()
        with pytest.raises(RemoteError) as invalid:
            await subscription.next(0)
    assert invalid.value.error.code is ErrorCode.INVALID_ARGUMENT


@pytest.mark.parametrize("release", ["close", "gc"])
async def test_subscription_capability_release_does_not_leak(
    tmp_path: Path,
    release: str,
) -> None:
    controller = FakeController()
    async with connected(tmp_path, controller) as client:
        subscription = await client.subscribe_logs()
        assert controller.master.logs._subscriptions
        if release == "close":
            await subscription.close()
        else:
            del subscription
            gc.collect()
        for _ in range(10):
            if not controller.master.logs._subscriptions:
                break
            await asyncio.sleep(0)
        assert not controller.master.logs._subscriptions


class RegistryController:
    def __init__(self, *, blocked: bool = False) -> None:
        self.registered: Any = None
        self.registration_entered = asyncio.Event()
        self.registration_release = asyncio.Event()
        if not blocked:
            self.registration_release.set()
        self.unregistered = asyncio.Event()
        self.registration_calls = 0

    async def register(self, remote: Any) -> None:
        self.registration_calls += 1
        self.registration_entered.set()
        await self.registration_release.wait()
        self.registered = remote

    async def unregister(self, remote: Any) -> bool:
        assert self.registered is remote
        self.unregistered.set()
        return True

    async def failed(self, remote: Any) -> bool:
        return self.registered is remote


async def open_registry(name: str) -> tuple[Any, Any, Any, asyncio.Future[Any]]:
    schema = load_schema()
    stream = await capnp.AsyncIoStream.create_unix_connection(f"\0{name}")
    client = capnp.TwoPartyClient(stream)
    disconnected = asyncio.ensure_future(client.on_disconnect())
    return (
        stream,
        client,
        client.bootstrap().cast_as(schema.WorkerRegistry),
        disconnected,
    )


async def test_registry_fingerprint_capability_and_disconnect_lifecycle() -> None:
    controller = RegistryController()
    incarnation = str(ULID())
    target = FakeShard(shard_status(incarnation=incarnation))
    servant = AgentServant(target)  # ty: ignore[invalid-argument-type]
    name = f"dst-registry-{ULID()}"

    async with (
        rpc_runtime(),
        abstract_rpc_server(
            lambda: WorkerRegistryServant(controller),  # ty: ignore[invalid-argument-type]
            name,
        ),
    ):
        stream, client, registry, disconnected = await open_registry(name)
        incompatible = await registry.register(
            schemaFingerprint="0" * 64,
            agent=servant,
        )
        with pytest.raises(RemoteError) as mismatch:
            unwrap_outcome(incompatible.result)
        assert mismatch.value.error.code is ErrorCode.INCOMPATIBLE_SCHEMA

        response = await registry.register(
            schemaFingerprint=SCHEMA_FINGERPRINT,
            agent=servant,
        )
        unwrap_outcome(response.result)
        assert controller.registration_calls == 1
        assert (
            controller.registered.name,
            controller.registered.master,
            controller.registered.incarnation,
        ) == ("Master", True, incarnation)

        assert await controller.registered.list_snapshots(7, before=0) == target.catalog
        assert target.snapshot_requests == [(7, 0)]
        await controller.registered.rollback_to_snapshot("SESSION", 31, 4)
        assert target.rollback_request == ("SESSION", 31, 4.0)
        target.rollback_error = TimeoutError()
        with pytest.raises(IndeterminateError):
            await controller.registered.rollback_to_snapshot("SESSION", 31, 4)

        duplicate = await registry.register(
            schemaFingerprint=SCHEMA_FINGERPRINT,
            agent=servant,
        )
        with pytest.raises(RemoteError) as repeated:
            unwrap_outcome(duplicate.result)
        assert repeated.value.error.code is ErrorCode.INVALID_STATE

        forwarded = controller.registered.logs.subscribe()
        record = log_record(1, "forwarded")
        target.logs.publish(record)
        async with asyncio.timeout(1):
            assert await forwarded.next(1) == (record,)
        forwarded.close()

        client.close()
        stream.close()
        async with asyncio.timeout(1):
            await disconnected
            await controller.unregistered.wait()

    await servant.aclose()
    assert not target.logs._subscriptions


async def test_disconnect_during_registration_rolls_back_capability() -> None:
    controller = RegistryController(blocked=True)
    target = FakeShard(shard_status(incarnation=str(ULID())))
    servant = AgentServant(target)  # ty: ignore[invalid-argument-type]
    name = f"dst-registry-race-{ULID()}"

    async with (
        rpc_runtime(),
        abstract_rpc_server(
            lambda: WorkerRegistryServant(controller),  # ty: ignore[invalid-argument-type]
            name,
        ),
    ):
        stream, client, registry, disconnected = await open_registry(name)
        pending = asyncio.ensure_future(
            registry.register(
                schemaFingerprint=SCHEMA_FINGERPRINT,
                agent=servant,
            )
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
