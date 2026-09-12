# ruff: file-ignore[blocking-path-method-in-async-function]
import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, cast

from ulid import ULID

from dst_server import commands as c
from dst_server.api import ClusterAPI, ShardAPI
from dst_server.cluster.subscriptions import (
    Broadcast,
    StreamKind,
    Subscription,
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
from dst_server.rpc.client import ClusterClient, rpc_runtime
from dst_server.rpc.servants import (
    BootstrapServant,
    ClusterEndpoint,
)
from dst_server.rpc.transport import filesystem_rpc_server

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

    def subscribe(self, kind: StreamKind) -> Subscription[Any]:
        return {
            "logs": self.logs,
            "lifecycle": self.lifecycle,
            "events": self.game_events,
        }[kind].subscribe()


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

    def subscribe(self, kind: StreamKind) -> Subscription[Any]:
        return self.master.subscribe(kind)


@asynccontextmanager
async def connected(
    tmp_path: Path, controller: ClusterEndpoint
) -> AsyncIterator[ClusterClient]:
    tmp_path.chmod(0o700)
    path = tmp_path / "cluster.sock"
    watchdog = asyncio.timeout(5)
    async with (
        watchdog,
        rpc_runtime(),
        filesystem_rpc_server(path, lambda: BootstrapServant(controller)),
        await ClusterClient.connect(path) as client,
    ):
        yield client
    assert not watchdog.expired(), "RPC test exceeded its watchdog"
