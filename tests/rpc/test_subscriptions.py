# ruff: file-ignore[blocking-path-method-in-async-function]
import asyncio
import gc
from pathlib import Path
from typing import Any
from weakref import ref

import pytest
from pydantic import ValidationError

from dst_server.cluster.subscriptions import (
    Broadcast,
    StreamKind,
    StreamRecord,
)
from dst_server.errors import (
    ErrorCode,
    RemoteError,
)
from dst_server.events.server import SavedEvent
from dst_server.events.world import ModOutdatedData, ModOutdatedEvent
from dst_server.models.cluster import (
    GameEventRecord,
    LifecycleRecord,
)
from dst_server.rpc.client import ClusterClient, rpc_runtime
from dst_server.rpc.codec import ERROR, decode
from dst_server.rpc.servants import (
    BootstrapServant,
)
from dst_server.rpc.transport import filesystem_rpc_server
from tests.cluster.helpers import controller as make_controller
from tests.rpc.helpers import FakeController, connected, log_record


async def test_subscription_kinds_reach_cluster_and_shard_clients(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    controller, master, _, _, _ = await make_controller(tmp_path, monkeypatch)
    log = log_record(1)
    fields = log.model_dump(exclude={"line"})
    records: dict[StreamKind, StreamRecord] = {
        "logs": log,
        "lifecycle": LifecycleRecord(
            **fields, event=SavedEvent(path="session/world/1", snapshot=1)
        ),
        "events": GameEventRecord(
            **fields,
            event=ModOutdatedEvent(
                v=2,
                nonce=str(log.game_attempt),
                generation=1,
                session_id="world",
                seq=1,
                event="dst.mod.outdated",
                tick=1,
                monotonic_ms=1,
                cycle=1,
                data=ModOutdatedData(name="Insight"),
            ),
        ),
    }
    sources: dict[StreamKind, Broadcast[Any]] = {
        "logs": master.logs,
        "lifecycle": master.lifecycle,
        "events": master.game_events,
    }
    try:
        async with connected(tmp_path, controller) as client:
            for kind, record in records.items():
                async with (
                    await client.subscribe(kind) as room,
                    await client.shard("Master").subscribe(kind) as shard,
                ):
                    sources[kind].publish(record)
                    async with asyncio.timeout(1):
                        assert await room.next() == await shard.next() == (record,)
    finally:
        await controller.aclose()


async def test_subscription_overflow_is_recoverable_and_close_releases(
    tmp_path: Path,
) -> None:
    controller = FakeController()
    async with connected(tmp_path, controller) as client:
        subscription = await client.subscribe("logs")
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
        subscription = await client.subscribe("logs")
        with pytest.raises(ValidationError):
            await subscription.next(0)
        response = await subscription._capability.next(maxItems=0)
        assert decode(ERROR, response.batch.error).code is ErrorCode.INVALID_ARGUMENT
        assert not controller.master.logs._subscriptions
        subscription = await client.subscribe("logs")
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
    async with (
        asyncio.timeout(5),
        rpc_runtime(),
        filesystem_rpc_server(path, bootstrap) as server,
    ):
        for sequence in range(10):
            async with (
                await ClusterClient.connect(path) as client,
                await client.subscribe("logs") as subscription,
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
