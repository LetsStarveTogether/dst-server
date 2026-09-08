# ruff: file-ignore[async-function-with-timeout, invalid-argument-name]
import asyncio
import inspect
from collections.abc import Awaitable, Callable
from contextlib import suppress
from importlib import import_module
from typing import Any, Protocol
from weakref import WeakSet, finalize

from logbook import Logger
from pydantic import BaseModel
from ulid import ULID

from dst_server import commands as c
from dst_server.cluster.subscriptions import Broadcast
from dst_server.cluster.subscriptions import Subscription as LocalSubscription
from dst_server.errors import (
    DisconnectedError,
    ErrorCode,
    ErrorInfo,
    RemoteError,
    SubscriptionOverflowError,
    error_info,
    indeterminate_cause,
    indeterminate_info,
)
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

from .client import RemoteEndpoint, StreamKind
from .client import Subscription as RemoteSubscription
from .codec import ERROR, encode, encode_model, failure, success
from .schema import load_schema

capnp: Any = import_module("capnp")
schema = load_schema()
logger = Logger(__name__)
_STREAM_COUNT = 3
_STREAM_RETRY_DELAY = 1.0

type _Operation = Callable[[], object | Awaitable[object]]
type _Encoder = Callable[[Any], object]


class Endpoint(Protocol):
    async def invoke[T](self, command: c.Request[T]) -> T: ...
    def subscribe_logs(self) -> LocalSubscription[LogRecord]: ...
    def subscribe_lifecycle(self) -> LocalSubscription[LifecycleRecord]: ...
    def subscribe_events(self) -> LocalSubscription[GameEventRecord]: ...


class ClusterEndpoint(Endpoint, Protocol):
    def shard(self, name: str) -> Endpoint: ...


class AgentEndpoint(Protocol):
    logs: Broadcast[LogRecord]
    lifecycle: Broadcast[LifecycleRecord]
    game_events: Broadcast[GameEventRecord]

    async def invoke[T](self, command: c.Request[T]) -> T: ...


class RegistryController(Protocol):
    async def register(self, endpoint: RemoteAgent) -> None: ...
    async def unregister(self, endpoint: RemoteAgent) -> bool: ...
    async def failed(self, endpoint: RemoteAgent) -> bool: ...


def _identity(value: object) -> object:
    return value


def _unit(_: object) -> None:
    return None


def _operation_error(method: str, error: BaseException) -> ErrorInfo:
    result = error_info(error)
    if result.code is ErrorCode.INTERNAL:
        logger.error(
            "RPC operation failed: {method}: {error_id}: {kind}",
            method=method,
            error_id=result.error_id,
            kind=type(error).__name__,
        )
    return result


def _internal_error(method: str, error: Exception) -> ErrorInfo:
    result = ErrorInfo(ErrorCode.INTERNAL, ULID(), "internal RPC error")
    logger.error(
        "RPC result encoding failed: {method}: {error_id}: {kind}",
        method=method,
        error_id=result.error_id,
        kind=type(error).__name__,
    )
    return result


async def _invoke(operation: _Operation) -> object:
    result = operation()
    return await result if inspect.isawaitable(result) else result


class _TaskOwner:
    def __init__(self) -> None:
        self._tasks: set[asyncio.Task[object]] = set()
        self._detached: set[asyncio.Task[object]] = set()
        self._subscriptions: WeakSet[_SubscriptionServant] = WeakSet()

    async def run(self, method: str, operation: _Operation) -> object:
        task = asyncio.create_task(_invoke(operation), name=f"dst-rpc-{method}")
        self._tasks.add(task)
        task.add_done_callback(lambda completed: self._done(method, completed))
        try:
            return await asyncio.shield(task)
        except asyncio.CancelledError:
            self._detached.add(task)
            if task not in self._tasks:
                self._detached.discard(task)
            raise

    def track(self, subscription: _SubscriptionServant) -> _SubscriptionServant:
        self._subscriptions.add(subscription)
        return subscription

    def discard(self, subscription: _SubscriptionServant) -> None:
        self._subscriptions.discard(subscription)

    async def aclose(self) -> None:
        for subscription in tuple(self._subscriptions):
            subscription.close_local()

    def _done(self, method: str, task: asyncio.Task[object]) -> None:
        self._tasks.discard(task)
        detached = task in self._detached
        self._detached.discard(task)
        if task.cancelled():
            return
        try:
            error = task.exception()
        except asyncio.CancelledError:
            return
        if detached and error is not None:
            logger.error(
                "detached RPC mutation failed: {method}: {error_id}: {kind}",
                method=method,
                error_id=ULID(),
                kind=type(error).__name__,
            )


class _Responder:
    def __init__(self, owner: _TaskOwner | None = None) -> None:
        self._owner = owner or _TaskOwner()

    async def _respond(
        self,
        context: Any,
        method: str,
        operation: _Operation,
        encode_result: _Encoder = _unit,
        *,
        mutation: bool = False,
    ) -> None:
        try:
            value = (
                await self._owner.run(method, operation)
                if mutation
                else await _invoke(operation)
            )
        except asyncio.CancelledError:
            raise
        except Exception as error:
            encoded_error = _operation_error(method, error)
            if mutation and indeterminate_cause(error):
                encoded_error = indeterminate_info(encoded_error)
            context.results.result = failure(encoded_error)
            return
        try:
            context.results.result = success(encode_result(value))
        except Exception as error:
            encoded_error = _internal_error(method, error)
            if mutation:
                encoded_error = indeterminate_info(encoded_error)
            context.results.result = failure(encoded_error)

    def _subscription(
        self, subscription: LocalSubscription[Any]
    ) -> _SubscriptionServant:
        return self._owner.track(
            _SubscriptionServant(subscription, self._owner.discard)
        )

    async def aclose(self) -> None:
        await self._owner.aclose()


class _SubscriptionServant(schema.Subscription.Server):
    def __init__(
        self,
        subscription: LocalSubscription[Any],
        discard: Callable[[_SubscriptionServant], None],
    ) -> None:
        self.subscription = subscription
        self._discard = discard
        self._finalizer = finalize(self, subscription.close)

    async def next(self, maxItems: int, _context: Any) -> None:
        try:
            items = await self.subscription.next(maxItems)
        except asyncio.CancelledError:
            raise
        except Exception as error:
            if not isinstance(error, SubscriptionOverflowError):
                self.close_local()
            _context.results.batch = {"error": encode(ERROR, error_info(error))}
            return
        if not items:
            self.close_local()
            _context.results.batch = {"closed": None}
            return
        try:
            _context.results.batch = {"items": [encode_model(item) for item in items]}
        except Exception as error:
            self.close_local()
            _context.results.batch = {
                "error": encode(ERROR, _internal_error("subscription.next", error))
            }

    async def close(self, _context: Any) -> None:
        self.close_local()
        _context.results.result = success()

    def close_local(self) -> None:
        self._finalizer()
        self._discard(self)


class _EndpointMethods(_Responder):
    def __init__(
        self,
        target: Endpoint | AgentEndpoint,
        scope: c.Scope,
        sources: dict[str, Callable[[], LocalSubscription[Any]]],
        owner: _TaskOwner | None = None,
    ) -> None:
        super().__init__(owner)
        self.target = target
        self.scope = scope
        self._sources = sources

    async def call_context(self, _context: Any) -> None:
        try:
            command = c.parse_request(_context.params.request, scope=self.scope)
            spec = c.operation(self.scope, command)
        except Exception as error:
            _context.results.result = failure(error_info(error))
            return
        finally:
            _context.release_params()

        async def invoke() -> object:
            async with asyncio.timeout(command.timeout + RPC_TIMEOUT_MARGIN):
                return await self.target.invoke(command)

        await self._respond(
            _context,
            command.method,
            invoke,
            lambda value: encode(spec.response, value),
            mutation=spec.mutation,
        )

    async def subscribe(self, kind: str, _context: Any) -> None:
        def subscribe() -> _SubscriptionServant:
            source = self._sources.get(kind)
            if source is None:
                msg = "unknown subscription kind"
                raise ValueError(msg)
            return self._subscription(source())

        await self._respond(_context, "subscribe", subscribe, _identity)


def _sources(target: Endpoint) -> dict[str, Callable[[], LocalSubscription[Any]]]:
    return {
        "logs": target.subscribe_logs,
        "lifecycle": target.subscribe_lifecycle,
        "events": target.subscribe_events,
    }


class ShardServant(_EndpointMethods, schema.Endpoint.Server):
    def __init__(self, target: Endpoint, owner: _TaskOwner | None = None) -> None:
        super().__init__(target, "shard", _sources(target), owner)


class AgentServant(_EndpointMethods, schema.Agent.Server):
    def __init__(self, target: AgentEndpoint) -> None:
        super().__init__(
            target,
            "agent",
            {
                "logs": target.logs.subscribe,
                "lifecycle": target.lifecycle.subscribe,
                "events": target.game_events.subscribe,
            },
        )


class ClusterServant(_EndpointMethods, schema.Cluster.Server):
    def __init__(
        self, controller: ClusterEndpoint, owner: _TaskOwner | None = None
    ) -> None:
        super().__init__(controller, "cluster", _sources(controller), owner)
        self.controller = controller
        self._shards: dict[str, ShardServant] = {}

    async def shard(self, shardName: str, _context: Any) -> None:
        def get() -> ShardServant:
            if shardName not in self._shards:
                self._shards[shardName] = ShardServant(
                    self.controller.shard(shardName), self._owner
                )
            return self._shards[shardName]

        await self._respond(_context, "shard", get, _identity)


class BootstrapServant(_Responder, schema.Bootstrap.Server):
    def __init__(self, controller: ClusterEndpoint) -> None:
        super().__init__()
        self.cluster = ClusterServant(controller, self._owner)

    async def connect(self, _context: Any) -> None:
        _context.results.result = success(self.cluster)


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
        self._subscriptions: list[RemoteSubscription[Any]] = []
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
        streams: tuple[tuple[StreamKind, type[BaseModel]], ...] = (
            ("logs", LogRecord),
            ("lifecycle", LifecycleRecord),
            ("events", GameEventRecord),
        )
        for kind, model in streams:
            self._subscriptions.append(await self._subscribe(kind, model))

    def start_pumps(self) -> None:
        if self._pumps or len(self._subscriptions) != _STREAM_COUNT:
            msg = "remote subscriptions are not ready"
            raise RuntimeError(msg)

        def start[T: BaseModel](
            index: int, kind: StreamKind, target: Broadcast[T], model: type[T]
        ) -> None:
            self._pumps.append(
                asyncio.create_task(
                    self._pump(self._subscriptions[index], kind, target, model),
                    name=f"dst-agent-{kind}",
                )
            )

        start(0, "logs", self.logs, LogRecord)
        start(1, "lifecycle", self.lifecycle, LifecycleRecord)
        start(2, "events", self.game_events, GameEventRecord)

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
                    *(self._close_subscription(item) for item in self._subscriptions)
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

    async def _release_subscription(
        self, subscription: RemoteSubscription[Any]
    ) -> None:
        with suppress(ValueError):
            self._subscriptions.remove(subscription)
        await self._close_subscription(subscription)

    async def _pump[RecordT: BaseModel](
        self,
        subscription: RemoteSubscription[RecordT],
        subscribe: StreamKind,
        target: Broadcast[RecordT],
        model: type[RecordT],
    ) -> None:
        current: RemoteSubscription[RecordT] | None = subscription
        while not self._closed:
            if current is None:
                await asyncio.sleep(_STREAM_RETRY_DELAY)
                try:
                    current = await self._subscribe(subscribe, model)
                except asyncio.CancelledError:
                    raise
                except Exception as error:
                    logger.warning(
                        "remote observation stream reconnect failed: "
                        "{shard}: {stream}: {kind}",
                        shard=self.name,
                        stream=model.__name__,
                        kind=type(error).__name__,
                    )
                    continue
                self._subscriptions.append(current)
            failure = "closed"
            try:
                await self._relay_stream(current, target, model)
            except asyncio.CancelledError:
                raise
            except Exception as error:
                failure = type(error).__name__
            finally:
                await self._release_subscription(current)
                current = None
            if not self._closed:
                logger.warning(
                    "remote observation stream ended; reconnecting: "
                    "{shard}: {stream}: {kind}",
                    shard=self.name,
                    stream=model.__name__,
                    kind=failure,
                )

    async def _relay_stream[RecordT: BaseModel](
        self,
        subscription: RemoteSubscription[RecordT],
        target: Broadcast[RecordT],
        model: type[RecordT],
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
                        stream=model.__name__,
                    )
                    continue
                raise
            if not records:
                return
            for record in records:
                target.publish(record)
            del records, record


class WorkerRegistryServant(_Responder, schema.WorkerRegistry.Server):
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
