import asyncio
from types import SimpleNamespace
from typing import Any, cast
from weakref import ref

import pytest
from ulid import ULID

from dst_server import commands as c
from dst_server.cluster.subscriptions import (
    Broadcast,
)
from dst_server.errors import (
    ErrorCode,
    RemoteError,
)
from dst_server.models.cluster import (
    LogRecord,
)
from dst_server.rpc.client import rpc_runtime
from dst_server.rpc.codec import unwrap_outcome
from dst_server.rpc.schema import load_schema
from dst_server.rpc.transport import abstract_rpc_server
from dst_server.rpc.workers import AgentServant, RemoteAgent, WorkerRegistryServant
from tests.helpers import wait_for_event
from tests.rpc.helpers import FakeShard, log_record

capnp: Any = pytest.importorskip("capnp")


async def test_remote_relay_releases_delivered_batch() -> None:
    agent = RemoteAgent(None)
    source, target = Broadcast[LogRecord](), Broadcast[LogRecord]()
    incoming, outgoing = source.subscribe(), target.subscribe()
    subscription: Any = SimpleNamespace(next=lambda: incoming.next(256))
    relay = asyncio.create_task(agent._relay_stream(subscription, target, "logs"))
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
        outgoing.close()
        async with asyncio.timeout(5):
            await relay
            await agent.aclose()


async def test_failed_remote_stream_exits_without_retaining_frames() -> None:
    agent = RemoteAgent(None)
    references = []

    class BrokenSubscription:
        async def next(self) -> None:
            record = log_record(0, "x" * 1024 * 1024)
            references.append(ref(record))
            msg = "stream failed"
            raise ValueError(msg)

        async def close(self) -> None:
            pass

    await agent._pump(cast("Any", BrokenSubscription()), agent.logs, "logs")
    assert references
    assert references[0]() is None
    await agent.aclose()


async def test_remote_agent_close_releases_capability() -> None:
    class Capability:
        pass

    capability = Capability()
    reference = ref(capability)
    agent = RemoteAgent(capability)
    del capability

    async with asyncio.timeout(5):
        await agent.aclose()

    assert reference() is None


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


async def test_registry_capability_and_disconnect_lifecycle() -> None:
    controller = RegistryController()
    target = FakeShard()
    servant = AgentServant(target)
    name = f"dst-registry-{ULID()}"
    try:
        async with (
            asyncio.timeout(5),
            rpc_runtime(),
            abstract_rpc_server(lambda: WorkerRegistryServant(controller), name),
        ):
            stream, client, registry, disconnected = await open_registry(name)
            try:
                unwrap_outcome((await registry.register(agent=servant)).result)
                remote = controller.registered
                assert (remote.name, remote.master, remote.incarnation) == (
                    "Master",
                    True,
                    str(target.value.agent_incarnation),
                )
                assert (
                    await remote.invoke(c.Snapshots(limit=7, before=0))
                    == target.catalog
                )
                assert target.requests[-1] == c.Snapshots(limit=7, before=0)
                with pytest.raises(RemoteError) as duplicate:
                    unwrap_outcome((await registry.register(agent=servant)).result)
                assert duplicate.value.error.code is ErrorCode.INVALID_STATE
                forwarded = remote.logs.subscribe()
                try:
                    record = log_record(1, "forwarded")
                    target.logs.publish(record)
                    assert await forwarded.next(1) == (record,)
                finally:
                    forwarded.close()
                unwrap_outcome((await registry.failed()).result)
            finally:
                client.close()
                stream.close()
            await disconnected
            await wait_for_event(controller.unregistered)
    finally:
        async with asyncio.timeout(5):
            await servant.aclose()
    assert not target.logs._subscriptions


async def test_disconnect_during_registration_rolls_back_capability() -> None:
    controller = RegistryController(blocked=True)
    target = FakeShard()
    servant = AgentServant(target)
    name = f"dst-registry-race-{ULID()}"
    try:
        async with (
            asyncio.timeout(5),
            rpc_runtime(),
            abstract_rpc_server(lambda: WorkerRegistryServant(controller), name),
        ):
            stream, client, registry, disconnected = await open_registry(name)
            pending = asyncio.ensure_future(registry.register(agent=servant))
            try:
                await wait_for_event(controller.registration_entered, pending)
                client.close()
                stream.close()
                await disconnected
                controller.registration_release.set()
                with pytest.raises(capnp.KjException):
                    await pending
                await wait_for_event(controller.unregistered)
            finally:
                controller.registration_release.set()
                client.close()
                stream.close()
                pending.cancel()
                async with asyncio.timeout(5):
                    await asyncio.gather(pending, return_exceptions=True)
    finally:
        async with asyncio.timeout(5):
            await servant.aclose()
    assert not target.logs._subscriptions
