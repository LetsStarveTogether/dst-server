# ruff: file-ignore[invalid-argument-name]
import asyncio
import inspect
from collections.abc import Awaitable, Callable
from typing import Any, Protocol
from weakref import WeakSet, finalize

from logbook import Logger
from ulid import ULID

from dst_server import commands as c
from dst_server.cluster.subscriptions import (
    STREAM_MODELS,
    StreamKind,
    StreamRecord,
)
from dst_server.cluster.subscriptions import Subscription as LocalSubscription
from dst_server.errors import (
    ErrorCode,
    ErrorInfo,
    IndeterminateError,
    SubscriptionOverflowError,
    error_info,
    indeterminate_info,
)
from dst_server.timeouts import (
    RPC_TIMEOUT_MARGIN,
)

from .codec import ERROR, encode, encode_model, failure, success
from .schema import load_schema

schema = load_schema()
logger = Logger(__name__)

type _Operation = Callable[[], object | Awaitable[object]]
type _Encoder = Callable[[Any], object]


class CommandEndpoint(Protocol):
    async def invoke[T](self, command: c.Request[T]) -> T: ...


class Endpoint(CommandEndpoint, Protocol):
    def subscribe(self, kind: StreamKind) -> LocalSubscription[StreamRecord]: ...


class ClusterEndpoint(Endpoint, Protocol):
    def shard(self, name: str) -> Endpoint: ...


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


class Responder:
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
            context.results.result = failure(_operation_error(method, error))
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


class EndpointMethods(Responder):
    def __init__(
        self,
        target: CommandEndpoint,
        scope: c.Scope,
        subscribe: Callable[[StreamKind], LocalSubscription[Any]],
        owner: _TaskOwner | None = None,
    ) -> None:
        super().__init__(owner)
        self.target = target
        self.scope = scope
        self._subscribe = subscribe

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
            watchdog = asyncio.timeout(command.timeout + RPC_TIMEOUT_MARGIN)
            try:
                async with watchdog:
                    return await self.target.invoke(command)
            except TimeoutError:
                if spec.mutation and watchdog.expired():
                    raise IndeterminateError from None
                raise

        await self._respond(
            _context,
            command.method,
            invoke,
            lambda value: encode(spec.response, value),
            mutation=spec.mutation,
        )

    async def subscribe(self, kind: str, _context: Any) -> None:
        def subscribe() -> _SubscriptionServant:
            if kind not in STREAM_MODELS:
                msg = "unknown subscription kind"
                raise ValueError(msg)
            return self._subscription(self._subscribe(kind))

        await self._respond(_context, "subscribe", subscribe, _identity)

    async def describe(self, _context: Any) -> None:
        await self._respond(
            _context,
            "describe",
            lambda: c.describe_operations(self.scope),
            lambda value: encode(c.METHOD_DESCRIPTIONS, value),
        )


class ShardServant(EndpointMethods, schema.Endpoint.Server):
    def __init__(self, target: Endpoint, owner: _TaskOwner | None = None) -> None:
        super().__init__(target, "shard", target.subscribe, owner)


class ClusterServant(EndpointMethods, schema.Cluster.Server):
    def __init__(
        self, controller: ClusterEndpoint, owner: _TaskOwner | None = None
    ) -> None:
        super().__init__(controller, "cluster", controller.subscribe, owner)
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


class BootstrapServant(Responder, schema.Bootstrap.Server):
    def __init__(self, controller: ClusterEndpoint) -> None:
        super().__init__()
        self.cluster = ClusterServant(controller, self._owner)

    async def connect(self, _context: Any) -> None:
        _context.results.result = success(self.cluster)
