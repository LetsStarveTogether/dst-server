# ruff: file-ignore[async-function-with-timeout]
import asyncio
from contextlib import suppress
from typing import Any, Protocol

from logbook import Logger
from pydantic import BaseModel

from dst_server import commands as c
from dst_server.cluster.subscriptions import STREAM_MODELS, Broadcast, StreamKind
from dst_server.errors import DisconnectedError, ErrorCode, RemoteError
from dst_server.models.cluster import (
    GameEventRecord,
    LifecycleRecord,
    LogRecord,
    ShardRuntimeStatus,
)
from dst_server.timeouts import (
    DEFAULT_CONNECT_TIMEOUT,
    RPC_TIMEOUT_MARGIN,
    positive_timeout,
)

from .client import RemoteEndpoint
from .client import Subscription as RemoteSubscription
from .schema import load_schema
from .servants import CommandEndpoint, EndpointMethods, Responder

schema = load_schema()
logger = Logger(__name__)


class AgentEndpoint(CommandEndpoint, Protocol):
    logs: Broadcast[LogRecord]
    lifecycle: Broadcast[LifecycleRecord]
    game_events: Broadcast[GameEventRecord]


class RegistryController(Protocol):
    async def register(self, endpoint: RemoteAgent) -> None: ...
    async def unregister(self, endpoint: RemoteAgent) -> bool: ...
    async def failed(self, endpoint: RemoteAgent) -> bool: ...


class AgentServant(EndpointMethods, schema.Agent.Server):
    def __init__(self, target: AgentEndpoint) -> None:
        sources = {
            "logs": target.logs,
            "lifecycle": target.lifecycle,
            "events": target.game_events,
        }
        super().__init__(target, "agent", lambda kind: sources[kind].subscribe())


class RemoteAgent(RemoteEndpoint):
    scope: c.Scope = "agent"

    def __init__(self, capability: Any) -> None:
        self.name = ""
        self.incarnation = ""
        self.master = False
        self.capability = capability
        self.logs = Broadcast[LogRecord]()
        self.lifecycle = Broadcast[LifecycleRecord]()
        self.game_events = Broadcast[GameEventRecord]()
        self._initial_status: ShardRuntimeStatus | None = None
        self._subscriptions: dict[StreamKind, RemoteSubscription[Any]] = {}
        self._pumps: list[asyncio.Task[None]] = []
        self._closed = False

    async def _get_capability(self) -> Any:
        if self._closed:
            msg = "remote shard agent is closed"
            raise DisconnectedError(msg)
        return self.capability

    async def open(self, timeout: float = DEFAULT_CONNECT_TIMEOUT) -> None:
        timeout = positive_timeout(timeout)
        try:
            async with asyncio.timeout(timeout):
                await self._open()
        except BaseException:
            await self.aclose()
            raise

    async def _open(self) -> None:
        status = await self.invoke(c.Status())
        if status.agent_incarnation is None:
            msg = "remote agent status has no incarnation"
            raise ValueError(msg)
        self.name = status.name
        self.incarnation = str(status.agent_incarnation)
        self.master = status.is_master
        self._initial_status = status
        for kind in STREAM_MODELS:
            self._subscriptions[kind] = await self.subscribe(kind)

    def start_pumps(self) -> None:
        if self._pumps or self._subscriptions.keys() != STREAM_MODELS.keys():
            msg = "remote subscriptions are not ready"
            raise RuntimeError(msg)
        targets = {
            "logs": self.logs,
            "lifecycle": self.lifecycle,
            "events": self.game_events,
        }
        self._pumps = [
            asyncio.create_task(
                self._pump(subscription, targets[kind], kind),
                name=f"dst-agent-{kind}",
            )
            for kind, subscription in self._subscriptions.items()
        ]

    async def runtime_status(self) -> ShardRuntimeStatus:
        if self._initial_status is not None:
            status, self._initial_status = self._initial_status, None
            return status
        return await self.invoke(c.Status())

    async def activate(self) -> None:
        await self.invoke(c.Activate())

    async def aclose(self) -> None:
        if self._closed:
            return
        self._closed = True
        for task in self._pumps:
            task.cancel()
        try:
            async with asyncio.timeout(RPC_TIMEOUT_MARGIN):
                await asyncio.gather(*self._pumps, return_exceptions=True)
                await asyncio.gather(
                    *(
                        self._close_subscription(item)
                        for item in self._subscriptions.values()
                    )
                )
        except TimeoutError:
            logger.warning("remote agent cleanup timed out: {shard}", shard=self.name)
        finally:
            self._pumps.clear()
            self._subscriptions.clear()
            self._initial_status = None
            self.capability = None
            self.logs.close()
            self.lifecycle.close()
            self.game_events.close()

    @staticmethod
    async def _close_subscription(subscription: RemoteSubscription[Any]) -> None:
        with suppress(Exception):
            async with asyncio.timeout(RPC_TIMEOUT_MARGIN):
                await subscription.close()

    async def _pump[RecordT: BaseModel](
        self,
        subscription: RemoteSubscription[RecordT],
        target: Broadcast[RecordT],
        kind: StreamKind,
    ) -> None:
        try:
            await self._relay_stream(subscription, target, kind)
        except Exception as error:
            logger.warning(
                "remote observation stream ended: {shard}: {stream}: {kind}",
                shard=self.name,
                stream=kind,
                kind=type(error).__name__,
            )
        finally:
            await self._close_subscription(subscription)

    async def _relay_stream[RecordT: BaseModel](
        self,
        subscription: RemoteSubscription[RecordT],
        target: Broadcast[RecordT],
        kind: StreamKind,
    ) -> None:
        while True:
            try:
                records = await subscription.next()
            except RemoteError as error:
                if error.error.code is ErrorCode.OVERFLOW:
                    logger.warning(
                        "remote subscription overflowed; records were dropped: "
                        "{shard}: {stream}",
                        shard=self.name,
                        stream=kind,
                    )
                    continue
                raise
            if not records:
                return
            for record in records:
                target.publish(record)
            del records, record


class WorkerRegistryServant(Responder, schema.WorkerRegistry.Server):
    def __init__(self, controller: RegistryController) -> None:
        super().__init__()
        self.controller = controller
        self.remote: RemoteAgent | None = None
        self._closed = False
        self._register_lock = asyncio.Lock()

    async def register(self, agent: Any, _context: Any) -> None:
        async def register() -> None:
            async with self._register_lock:
                if self._closed or self.remote is not None:
                    msg = "registry connection already registered an agent"
                    raise RuntimeError(msg)
                remote = RemoteAgent(agent)
                registered = False
                try:
                    await remote.open()
                    await self.controller.register(remote)
                    registered = True
                    self._accept_remote(remote)
                except BaseException:
                    try:
                        if registered:
                            await self.controller.unregister(remote)
                    finally:
                        await remote.aclose()
                    raise

        await self._respond(_context, "register", register, mutation=True)

    def _accept_remote(self, remote: RemoteAgent) -> None:
        if self._closed:
            msg = "registry connection closed during registration"
            raise RuntimeError(msg)
        remote.start_pumps()
        self.remote = remote

    async def failed(self, _context: Any) -> None:
        async def failed() -> None:
            remote = self.remote
            if remote is None:
                msg = "registry connection has no registered agent"
                raise RuntimeError(msg)
            if not await self.controller.failed(remote):
                msg = "registered agent is no longer current"
                raise RuntimeError(msg)

        await self._respond(_context, "failed", failed, mutation=True)

    async def aclose(self) -> None:
        if self._closed:
            return
        self._closed = True
        await self._owner.aclose()
        remote, self.remote = self.remote, None
        if remote is None:
            return
        try:
            await self.controller.unregister(remote)
        finally:
            await remote.aclose()
