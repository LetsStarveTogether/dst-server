# ruff: file-ignore[async-function-with-timeout, invalid-argument-name, invalid-function-name]
import asyncio
import inspect
from collections.abc import Awaitable, Callable, Iterable
from contextlib import suppress
from importlib import import_module
from typing import Any
from weakref import WeakSet, finalize

from logbook import Logger
from pydantic import BaseModel, ValidationError
from ulid import ULID

from dst_server.cluster.agent import ShardAgent
from dst_server.cluster.config import ClusterConfig
from dst_server.cluster.configuration import (
    ConfigurationSnapshot,
    ConfigurationWriteError,
    GamesRunningError,
    InvalidConfiguration,
    InvalidConfigurationError,
    RevisionConflictError,
    TopologyChangeError,
)
from dst_server.cluster.controller import (
    ClusterController,
    ControllerOperationError,
    IncompleteRosterError,
    PlayerLocationConflictError,
    ShardController,
)
from dst_server.cluster.subscriptions import (
    Broadcast,
    SubscriptionOverflowError,
)
from dst_server.cluster.subscriptions import (
    Subscription as LocalSubscription,
)
from dst_server.events.server import SavedEvent
from dst_server.game.validation import number as _validate_number
from dst_server.game.validation import positive_timeout as _timeout
from dst_server.models import Inventory, Player
from dst_server.runtime import IndeterminateCommandError

from .client import (
    DEFAULT_OPERATION_TIMEOUT,
    RemotePlayerClient,
    _ShardClient,
)
from .client import (
    Subscription as RemoteSubscription,
)
from .codec import decode_model, encode_json_value, encode_model
from .errors import (
    DisconnectedError,
    ErrorCode,
    ErrorInfo,
    RemoteError,
    failure,
    success,
)
from .models import (
    GameEventRecord,
    LifecycleRecord,
    LogRecord,
    ShardRuntimeStatus,
)
from .schema import SCHEMA_FINGERPRINT, load_schema

capnp: Any = import_module("capnp")
schema = load_schema()
logger = Logger(__name__)
_STREAM_COUNT = 3
_HANDSHAKE_TIMEOUT = 30.0
_CLOSE_TIMEOUT = 5.0
_STREAM_RETRY_DELAY = 1.0

type _Operation = Callable[[], object | Awaitable[object]]
type _Encoder = Callable[[Any], object]


def _identity(value: object) -> object:
    return value


def _unit(_: object) -> None:
    return None


def _scalar(value: object) -> dict[str, object]:
    return {"value": value}


def _models(values: Iterable[BaseModel]) -> list[bytes]:
    return [encode_model(value) for value in values]


def _configuration_read(
    value: ConfigurationSnapshot | InvalidConfiguration,
) -> dict[str, object]:
    if isinstance(value, InvalidConfiguration):
        return {
            "invalid": {
                "revision": value.revision,
                "fields": [{"components": list(path)} for path in value.paths],
            }
        }
    return {
        "valid": {
            "revision": value.revision,
            "configuration": encode_model(value.configuration),
        }
    }


def _shard_results(method: str, values: Iterable[Any]) -> list[dict[str, object]]:
    def result(value: Any) -> dict[str, object]:
        if value.error is None:
            return success(_scalar(value.value))
        error = _operation_error(method, value.error)
        if _indeterminate_cause(value.error):
            error = _indeterminate_info(error)
        return failure(error)

    return [
        {
            "shard": value.shard,
            "result": result(value),
        }
        for value in values
    ]


def _nullable(value: object | None, encode: _Encoder = _identity) -> dict[str, object]:
    return {"none": None} if value is None else {"value": encode(value)}


def _decode_nullable(value: Any, decode: Callable[[Any], Any] = _identity) -> Any:
    selected = value.which()
    if selected == "none":
        return None
    if selected != "value":
        msg = f"invalid nullable RPC member: {selected}"
        raise ValueError(msg)
    return decode(value.value)


def _number(value: float) -> float:
    return _validate_number("number", value)


def _canonical_ulid(value: str) -> str:
    result = str(ULID.from_str(value))
    if result != value:
        msg = "ULID must use its canonical representation"
        raise ValueError(msg)
    return result


def _field_paths(error: BaseException) -> tuple[str, ...]:
    if isinstance(error, ValidationError):
        return tuple(
            ".".join(map(str, item["loc"]))
            for item in error.errors(include_input=False, include_url=False)
        )
    paths = getattr(error, "paths", ())
    return tuple(".".join(map(str, path)) for path in paths)


def _error_info(error: BaseException) -> ErrorInfo:  # ruff: ignore[complex-structure, too-many-branches]
    error_id = getattr(error, "error_id", None) or ULID()
    fields = _field_paths(error)
    if isinstance(error, RemoteError):
        return error.error
    if isinstance(error, ValidationError | ValueError | TypeError):
        return ErrorInfo(
            ErrorCode.INVALID_ARGUMENT, error_id, "invalid argument", fields
        )
    if isinstance(error, KeyError):
        return ErrorInfo(ErrorCode.NOT_FOUND, error_id, "resource not found")
    if isinstance(error, RevisionConflictError):
        return ErrorInfo(ErrorCode.CONFLICT, error_id, "configuration changed")
    if isinstance(error, GamesRunningError):
        return ErrorInfo(
            ErrorCode.INVALID_STATE, error_id, "game processes are running"
        )
    if isinstance(error, InvalidConfigurationError):
        return ErrorInfo(
            ErrorCode.INVALID_ARGUMENT,
            error_id,
            "configuration is invalid",
            fields,
        )
    if isinstance(error, TopologyChangeError):
        return ErrorInfo(
            ErrorCode.TOPOLOGY_CHANGE_REQUIRED,
            error_id,
            "configuration changes deployment topology",
            fields,
        )
    if isinstance(error, TimeoutError):
        return ErrorInfo(ErrorCode.TIMEOUT, error_id, "operation timed out")
    if isinstance(error, SubscriptionOverflowError):
        return ErrorInfo(ErrorCode.OVERFLOW, error_id, "subscription overflowed")
    if isinstance(error, PlayerLocationConflictError):
        return ErrorInfo(ErrorCode.CONFLICT, error_id, "player location conflicts")
    if isinstance(error, IncompleteRosterError):
        return ErrorInfo(
            ErrorCode.INVALID_STATE,
            error_id,
            "cluster shard roster is incomplete",
        )
    if isinstance(error, IndeterminateCommandError):
        return ErrorInfo(
            ErrorCode.INDETERMINATE, error_id, "operation is indeterminate"
        )
    if isinstance(error, DisconnectedError):
        return ErrorInfo(ErrorCode.UNAVAILABLE, error_id, "shard agent is unavailable")
    if isinstance(error, RuntimeError) and not isinstance(
        error, ControllerOperationError | ConfigurationWriteError
    ):
        return ErrorInfo(ErrorCode.INVALID_STATE, error_id, "operation is invalid now")
    return ErrorInfo(ErrorCode.INTERNAL, error_id, "internal RPC error", fields)


def _internal_error(method: str, error: Exception) -> ErrorInfo:
    result = ErrorInfo(ErrorCode.INTERNAL, ULID(), "internal RPC error")
    logger.error(
        "RPC result encoding failed: {method}: {error_id}: {kind}",
        method=method,
        error_id=result.error_id,
        kind=type(error).__name__,
    )
    return result


def _operation_error(method: str, error: BaseException) -> ErrorInfo:
    result = _error_info(error)
    if result.code is ErrorCode.INTERNAL:
        logger.error(
            "RPC operation failed: {method}: {error_id}: {kind}",
            method=method,
            error_id=result.error_id,
            kind=type(error).__name__,
        )
    return result


def _indeterminate_info(error: ErrorInfo) -> ErrorInfo:
    return ErrorInfo(
        ErrorCode.INDETERMINATE,
        error.error_id,
        "operation is indeterminate",
        error.fields,
    )


def _indeterminate_cause(error: BaseException) -> bool:
    if isinstance(error, TimeoutError | IndeterminateCommandError):
        return True
    if isinstance(error, RemoteError):
        return error.error.code in {ErrorCode.TIMEOUT, ErrorCode.INDETERMINATE}
    return isinstance(error, BaseExceptionGroup) and any(
        _indeterminate_cause(nested) for nested in error.exceptions
    )


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
        encode: _Encoder = _unit,
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
            if mutation and _indeterminate_cause(error):
                encoded_error = _indeterminate_info(encoded_error)
            context.results.result = failure(encoded_error)
            return
        try:
            context.results.result = success(encode(value))
        except Exception as error:
            encoded_error = _internal_error(method, error)
            if mutation:
                encoded_error = _indeterminate_info(encoded_error)
            context.results.result = failure(encoded_error)
            return

    def _subscription[ItemT](
        self,
        subscription: LocalSubscription[ItemT],
    ) -> _SubscriptionServant:
        return self._owner.track(
            _SubscriptionServant(subscription, self._owner.discard)
        )


class _SubscriptionServant(schema.DataSubscription.Server):
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
            _context.results.batch = {"error": _error_info(error).to_wire()}
            return
        if not items:
            self.close_local()
            _context.results.batch = {"closed": None}
            return
        try:
            encoded = [encode_model(item) for item in items]
        except Exception as error:
            self.close_local()
            _context.results.batch = {
                "error": _internal_error("subscription.next", error).to_wire()
            }
            return
        _context.results.batch = {"items": encoded}

    async def close(self, _context: Any) -> None:
        self.close_local()
        _context.results.result = success()

    async def aclose(self) -> None:
        self.close_local()

    def close_local(self) -> None:
        self._finalizer()
        self._discard(self)


class _ShardMethods(_Responder):  # ruff: ignore[too-many-public-methods]
    target: ShardController | ShardAgent

    async def status(self, _context: Any) -> None:
        await self._respond(
            _context,
            "status",
            self.target.runtime_status
            if isinstance(self.target, ShardAgent)
            else self.target.status,
            encode_model,
        )

    async def start(self, _context: Any) -> None:
        await self._respond(_context, "start", self.target.start, mutation=True)

    async def stop(self, _context: Any) -> None:
        await self._respond(_context, "stop", self.target.stop, mutation=True)

    async def restart(self, _context: Any) -> None:
        await self._respond(_context, "restart", self.target.restart, mutation=True)

    async def kill(self, _context: Any) -> None:
        await self._respond(_context, "kill", self.target.kill, mutation=True)

    async def execute(self, source: str, timeout: float, _context: Any) -> None:
        await self._respond(
            _context,
            "execute",
            lambda: self.target.execute(source, _timeout(timeout)),
            lambda value: _scalar(str(value)),
            mutation=True,
        )

    async def save(self, timeout: float, _context: Any) -> None:
        await self._respond(
            _context,
            "save",
            lambda: self.target.save(_timeout(timeout)),
            encode_model,
            mutation=True,
        )

    async def executeJson(self, source: str, _context: Any) -> None:
        await self._respond(
            _context,
            "executeJson",
            lambda: self.target.execute_json(source),
            encode_json_value,
            mutation=True,
        )

    async def health(self, _context: Any) -> None:
        await self._respond(_context, "health", self.target.health, encode_model)

    async def room(self, _context: Any) -> None:
        await self._respond(_context, "room", self.target.room, encode_model)

    async def world(self, _context: Any) -> None:
        await self._respond(_context, "world", self.target.world, encode_model)

    async def runtime(self, _context: Any) -> None:
        await self._respond(_context, "runtime", self.target.runtime, encode_model)

    async def mods(self, _context: Any) -> None:
        await self._respond(_context, "mods", self.target.mods, _models)

    async def connectedShards(self, _context: Any) -> None:
        await self._respond(
            _context, "connectedShards", self.target.connected_shards, _models
        )

    async def pause(self, paused: bool, _context: Any) -> None:
        await self._respond(
            _context,
            "pause",
            lambda: self.target.pause(paused),
            _scalar,
            mutation=True,
        )

    async def regenerateShard(
        self,
        preserveSettings: bool,
        timeout: float,
        _context: Any,
    ) -> None:
        await self._respond(
            _context,
            "regenerateShard",
            lambda: self.target.regenerate_shard(
                preserve_settings=preserveSettings,
                completion_timeout=_timeout(timeout),
            ),
            mutation=True,
        )

    async def listPlayers(self, _context: Any) -> None:
        await self._respond(_context, "listPlayers", self.target.list_players, _models)

    async def getPlayer(self, userid: str, _context: Any) -> None:
        await self._respond(
            _context,
            "getPlayer",
            lambda: self.target.get_player(userid),
            lambda value: _nullable(value, encode_model),
        )

    async def inventory(self, userid: str, _context: Any) -> None:
        await self._respond(
            _context,
            "inventory",
            lambda: self.target.inventory(userid),
            lambda value: _nullable(value, encode_model),
        )

    async def kick(self, userid: str, _context: Any) -> None:
        await self._respond(
            _context, "kick", lambda: self.target.kick(userid), mutation=True
        )

    async def ban(self, userid: str, seconds: Any, _context: Any) -> None:
        await self._respond(
            _context,
            "ban",
            lambda: self.target.ban(
                userid,
                seconds=_decode_nullable(seconds, lambda value: int(value.value)),
            ),
            mutation=True,
        )

    async def blocklist(self, _context: Any) -> None:
        await self._respond(_context, "blocklist", self.target.blocklist, list)

    async def isBlocked(self, userid: str, _context: Any) -> None:
        await self._respond(
            _context, "isBlocked", lambda: self.target.is_blocked(userid), _scalar
        )

    async def unban(self, userid: str, _context: Any) -> None:
        await self._respond(
            _context,
            "unban",
            lambda: self.target.unban(userid),
            _scalar,
            mutation=True,
        )

    async def isAdmin(self, userid: str, _context: Any) -> None:
        await self._respond(
            _context,
            "isAdmin",
            lambda: self.target.is_admin(userid),
            lambda value: _nullable(value, _scalar),
        )

    async def setVitals(
        self,
        userid: str,
        health: Any,
        hunger: Any,
        sanity: Any,
        temperature: Any,
        moisture: Any,
        _context: Any,
    ) -> None:
        def decode(value: Any) -> float:
            return _number(float(value.value))

        await self._respond(
            _context,
            "setVitals",
            lambda: self.target.set_vitals(
                userid,
                health=_decode_nullable(health, decode),
                hunger=_decode_nullable(hunger, decode),
                sanity=_decode_nullable(sanity, decode),
                temperature=_decode_nullable(temperature, decode),
                moisture=_decode_nullable(moisture, decode),
            ),
            _scalar,
            mutation=True,
        )

    async def killPlayer(self, userid: str, _context: Any) -> None:
        await self._respond(
            _context,
            "killPlayer",
            lambda: self.target.kill_player(userid),
            _scalar,
            mutation=True,
        )

    async def revive(self, userid: str, _context: Any) -> None:
        await self._respond(
            _context,
            "revive",
            lambda: self.target.revive(userid),
            _scalar,
            mutation=True,
        )

    async def despawn(self, userid: str, _context: Any) -> None:
        await self._respond(
            _context,
            "despawn",
            lambda: self.target.despawn(userid),
            _scalar,
            mutation=True,
        )

    async def migrate(
        self,
        userid: str,
        shardId: str,
        portalId: int,
        _context: Any,
    ) -> None:
        await self._respond(
            _context,
            "migrate",
            lambda: self.target.migrate(userid, shardId, portalId),
            _scalar,
            mutation=True,
        )

    async def teleport(
        self,
        userid: str,
        x: float,
        y: float,
        z: float,
        _context: Any,
    ) -> None:
        await self._respond(
            _context,
            "teleport",
            lambda: self.target.teleport(userid, _number(x), _number(y), _number(z)),
            _scalar,
            mutation=True,
        )

    async def give(self, userid: str, item: str, count: int, _context: Any) -> None:
        await self._respond(
            _context,
            "give",
            lambda: self.target.give(userid, item, count),
            _scalar,
            mutation=True,
        )

    async def remove(self, userid: str, item: str, count: int, _context: Any) -> None:
        await self._respond(
            _context,
            "remove",
            lambda: self.target.remove(userid, item, count),
            _scalar,
            mutation=True,
        )

    async def subscribeLogs(self, _context: Any) -> None:
        await self._respond(
            _context,
            "subscribeLogs",
            lambda: self._subscribe("logs"),
            _identity,
        )

    async def subscribeLifecycle(self, _context: Any) -> None:
        await self._respond(
            _context,
            "subscribeLifecycle",
            lambda: self._subscribe("lifecycle"),
            _identity,
        )

    async def subscribeEvents(self, _context: Any) -> None:
        await self._respond(
            _context,
            "subscribeEvents",
            lambda: self._subscribe("events"),
            _identity,
        )

    def _subscribe(self, kind: str) -> _SubscriptionServant:
        if isinstance(self.target, ShardAgent):
            source = getattr(self.target, "game_events" if kind == "events" else kind)
            subscription = source.subscribe()
        else:
            subscription = getattr(self.target, f"subscribe_{kind}")()
        return self._subscription(subscription)


class ShardServant(_ShardMethods, schema.Shard.Server):
    target: ShardController

    def __init__(
        self, target: ShardController, owner: _TaskOwner | None = None
    ) -> None:
        super().__init__(owner)
        self.target = target


class AgentServant(_ShardMethods, schema.Agent.Server):
    target: ShardAgent

    def __init__(self, target: ShardAgent) -> None:
        super().__init__()
        self.target = target

    async def saveMarker(self, _context: Any) -> None:
        await self._respond(_context, "saveMarker", self.target.save_marker, _scalar)

    async def waitSaved(
        self,
        afterSequence: int,
        snapshot: Any,
        timeout: float,
        _context: Any,
    ) -> None:
        await self._respond(
            _context,
            "waitSaved",
            lambda: self.target.wait_saved(
                afterSequence,
                _decode_nullable(snapshot, lambda value: int(value.value)),
                _timeout(timeout),
            ),
            encode_model,
        )

    async def generationMarker(self, _context: Any) -> None:
        await self._respond(
            _context, "generationMarker", self.target.generation_marker, _scalar
        )

    async def waitGeneration(
        self, afterGeneration: int, timeout: float, _context: Any
    ) -> None:
        await self._respond(
            _context,
            "waitGeneration",
            lambda: self.target.wait_generation(afterGeneration, _timeout(timeout)),
            _scalar,
        )

    async def isWhitelisted(self, userid: str, _context: Any) -> None:
        await self._respond(
            _context,
            "isWhitelisted",
            lambda: self.target.is_whitelisted(userid),
            _scalar,
        )

    async def whitelist(self, userid: str, _context: Any) -> None:
        await self._respond(
            _context,
            "whitelist",
            lambda: self.target.whitelist(userid),
            _scalar,
            mutation=True,
        )

    async def unwhitelist(self, userid: str, _context: Any) -> None:
        await self._respond(
            _context,
            "unwhitelist",
            lambda: self.target.unwhitelist(userid),
            _scalar,
            mutation=True,
        )

    async def activate(self, _context: Any) -> None:
        await self._respond(_context, "activate", self.target.activate, mutation=True)

    async def announce(self, message: str, _context: Any) -> None:
        await self._respond(
            _context,
            "announce",
            lambda: self.target.announce(message),
            mutation=True,
        )

    async def reset(self, timeout: float, _context: Any) -> None:
        await self._respond(
            _context,
            "reset",
            lambda: self.target.reset(_timeout(timeout)),
            mutation=True,
        )

    async def rollback(self, count: int, timeout: float, _context: Any) -> None:
        await self._respond(
            _context,
            "rollback",
            lambda: self.target.rollback(count, _timeout(timeout)),
            mutation=True,
        )

    async def regenerate(self, timeout: float, _context: Any) -> None:
        await self._respond(
            _context,
            "regenerate",
            lambda: self.target.regenerate(_timeout(timeout)),
            mutation=True,
        )

    async def aclose(self) -> None:
        await self._owner.aclose()


class ClusterServant(_Responder, schema.Cluster.Server):  # ruff: ignore[too-many-public-methods]
    def __init__(
        self,
        controller: ClusterController,
        owner: _TaskOwner | None = None,
    ) -> None:
        super().__init__(owner)
        self.controller = controller
        self._shards: dict[str, ShardServant] = {}

    async def status(self, _context: Any) -> None:
        await self._respond(_context, "status", self.controller.status, encode_model)

    async def start(self, _context: Any) -> None:
        await self._respond(_context, "start", self.controller.start, mutation=True)

    async def stop(self, _context: Any) -> None:
        await self._respond(_context, "stop", self.controller.stop, mutation=True)

    async def restart(self, _context: Any) -> None:
        await self._respond(_context, "restart", self.controller.restart, mutation=True)

    async def kill(self, _context: Any) -> None:
        await self._respond(_context, "kill", self.controller.kill, mutation=True)

    async def updateMods(self, _context: Any) -> None:
        await self._respond(
            _context, "updateMods", self.controller.update_mods, mutation=True
        )

    async def readConfiguration(self, _context: Any) -> None:
        await self._respond(
            _context,
            "readConfiguration",
            self.controller.read_configuration,
            _configuration_read,
        )

    async def saveConfiguration(
        self,
        expectedRevision: str,
        configuration: Any,
        _context: Any,
    ) -> None:
        await self._respond(
            _context,
            "saveConfiguration",
            lambda: self.controller.save_configuration(
                _canonical_ulid(expectedRevision),
                decode_model(ClusterConfig, configuration),
            ),
            lambda value: {
                "revision": value.revision,
                "configuration": encode_model(value.configuration),
            },
            mutation=True,
        )

    async def executeAll(self, source: str, timeout: float, _context: Any) -> None:
        await self._respond(
            _context,
            "executeAll",
            lambda: self.controller.execute_all(source, _timeout(timeout)),
            lambda values: _shard_results("executeAll", values),
            mutation=True,
        )

    async def announce(self, message: str, _context: Any) -> None:
        await self._respond(
            _context,
            "announce",
            lambda: self.controller.announce(message),
            mutation=True,
        )

    async def save(self, timeout: float, _context: Any) -> None:
        await self._respond(
            _context,
            "save",
            lambda: self.controller.save(_timeout(timeout)),
            lambda value: {
                "snapshot": _nullable(value.snapshot, _scalar),
                "shards": [
                    {"shard": shard, "event": encode_model(event)}
                    for shard, event in value.shards
                ],
            },
            mutation=True,
        )

    async def pause(self, paused: bool, _context: Any) -> None:
        await self._respond(
            _context,
            "pause",
            lambda: self.controller.pause(paused),
            lambda values: _shard_results("pause", values),
            mutation=True,
        )

    async def reset(self, timeout: float, _context: Any) -> None:
        await self._respond(
            _context,
            "reset",
            lambda: self.controller.reset(_timeout(timeout)),
            mutation=True,
        )

    async def rollback(self, count: int, timeout: float, _context: Any) -> None:
        await self._respond(
            _context,
            "rollback",
            lambda: self.controller.rollback(count, _timeout(timeout)),
            mutation=True,
        )

    async def regenerate(self, timeout: float, _context: Any) -> None:
        await self._respond(
            _context,
            "regenerate",
            lambda: self.controller.regenerate(_timeout(timeout)),
            mutation=True,
        )

    async def listPlayers(self, _context: Any) -> None:
        await self._respond(
            _context, "listPlayers", self.controller.list_players, _models
        )

    async def getPlayer(self, userid: str, _context: Any) -> None:
        await self._respond(
            _context,
            "getPlayer",
            lambda: self.controller.get_player(userid),
            lambda value: _nullable(value, encode_model),
        )

    async def isWhitelisted(self, userid: str, _context: Any) -> None:
        await self._respond(
            _context,
            "isWhitelisted",
            lambda: self.controller.is_whitelisted(userid),
            _scalar,
        )

    async def whitelist(self, userid: str, _context: Any) -> None:
        await self._respond(
            _context,
            "whitelist",
            lambda: self.controller.whitelist(userid),
            _scalar,
            mutation=True,
        )

    async def unwhitelist(self, userid: str, _context: Any) -> None:
        await self._respond(
            _context,
            "unwhitelist",
            lambda: self.controller.unwhitelist(userid),
            _scalar,
            mutation=True,
        )

    async def subscribeLogs(self, _context: Any) -> None:
        await self._respond(
            _context,
            "subscribeLogs",
            lambda: self._subscription(self.controller.subscribe_logs()),
            _identity,
        )

    async def subscribeLifecycle(self, _context: Any) -> None:
        await self._respond(
            _context,
            "subscribeLifecycle",
            lambda: self._subscription(self.controller.subscribe_lifecycle()),
            _identity,
        )

    async def subscribeEvents(self, _context: Any) -> None:
        await self._respond(
            _context,
            "subscribeEvents",
            lambda: self._subscription(self.controller.subscribe_events()),
            _identity,
        )

    async def shard(self, shardName: str, _context: Any) -> None:
        def get() -> ShardServant:
            target = self.controller.shard(shardName)
            return self._shards.setdefault(shardName, ShardServant(target, self._owner))

        await self._respond(_context, "shard", get, _identity)


class BootstrapServant(_Responder, schema.Bootstrap.Server):
    def __init__(self, controller: ClusterController) -> None:
        owner = _TaskOwner()
        super().__init__(owner)
        self.cluster = ClusterServant(controller, owner)

    async def connect(self, schemaFingerprint: str, _context: Any) -> None:
        if schemaFingerprint != SCHEMA_FINGERPRINT:
            _context.results.result = failure(
                ErrorInfo(
                    ErrorCode.INCOMPATIBLE_SCHEMA,
                    ULID(),
                    "RPC schema is incompatible",
                )
            )
            return
        _context.results.result = success(self.cluster)

    async def aclose(self) -> None:
        await self._owner.aclose()


class RemoteAgent(_ShardClient):  # ruff: ignore[too-many-public-methods]
    def __init__(self, capability: Any) -> None:
        self.name = ""
        self.incarnation = ""
        self.master = False
        self.capability = capability
        self.players = RemotePlayerClient(self)
        self.logs = Broadcast[LogRecord]()
        self.lifecycle = Broadcast[LifecycleRecord]()
        self.game_events = Broadcast[GameEventRecord]()
        self._initial_status: ShardRuntimeStatus | None = None
        self._subscriptions: list[RemoteSubscription[Any]] = []
        self._pumps: list[asyncio.Task[None]] = []
        self._closed = False

    async def open(self) -> None:
        try:
            async with asyncio.timeout(_HANDSHAKE_TIMEOUT):
                await self._open()
        except BaseException:
            await self.aclose()
            raise

    async def _open(self) -> None:
        status = await self._call(
            "status", decode=lambda value: decode_model(ShardRuntimeStatus, value)
        )
        if status.agent_incarnation is None:
            msg = "remote agent status has no incarnation"
            raise ValueError(msg)
        self.name = status.name
        self.incarnation = str(status.agent_incarnation)
        self.master = status.is_master
        self._initial_status = status
        self._subscriptions.extend((
            await self._subscribe("subscribeLogs", LogRecord),
            await self._subscribe("subscribeLifecycle", LifecycleRecord),
            await self._subscribe("subscribeEvents", GameEventRecord),
        ))

    def start_pumps(self) -> None:
        if self._pumps or len(self._subscriptions) != _STREAM_COUNT:
            msg = "remote subscriptions are not ready"
            raise RuntimeError(msg)
        logs, lifecycle, events = self._subscriptions
        self._pumps.extend((
            asyncio.create_task(
                self._pump(logs, "subscribeLogs", self.logs, LogRecord),
                name="dst-agent-subscribeLogs",
            ),
            asyncio.create_task(
                self._pump(
                    lifecycle,
                    "subscribeLifecycle",
                    self.lifecycle,
                    LifecycleRecord,
                ),
                name="dst-agent-subscribeLifecycle",
            ),
            asyncio.create_task(
                self._pump(
                    events,
                    "subscribeEvents",
                    self.game_events,
                    GameEventRecord,
                ),
                name="dst-agent-subscribeEvents",
            ),
        ))

    async def runtime_status(self) -> ShardRuntimeStatus:
        if self._initial_status is not None:
            status, self._initial_status = self._initial_status, None
            return status
        return await self._call(
            "status", decode=lambda value: decode_model(ShardRuntimeStatus, value)
        )

    async def activate(self) -> None:
        await self._call("activate", mutation=True)

    async def regenerate_shard(
        self,
        *,
        preserve_settings: bool,
        completion_timeout: float,
    ) -> None:
        await self._call(
            "regenerateShard",
            mutation=True,
            preserveSettings=preserve_settings,
            timeout=completion_timeout,
        )

    async def list_players(self) -> tuple[Player, ...]:
        return await self.players.list()

    async def get_player(self, userid: str) -> Player | None:
        return await self.players.get(userid)

    async def inventory(self, userid: str) -> Inventory | None:
        return await self.players.inventory(userid)

    async def kick(self, userid: str) -> None:
        await self.players.kick(userid)

    async def ban(self, userid: str, *, seconds: int | None = None) -> None:
        await self.players.ban(userid, seconds=seconds)

    async def blocklist(self) -> tuple[str, ...]:
        return await self.players.blocklist()

    async def is_blocked(self, userid: str) -> bool:
        return await self.players.is_blocked(userid)

    async def unban(self, userid: str) -> bool:
        return await self.players.unban(userid)

    async def is_admin(self, userid: str) -> bool | None:
        return await self.players.is_admin(userid)

    async def set_vitals(
        self,
        userid: str,
        *,
        health: float | None,
        hunger: float | None,
        sanity: float | None,
        temperature: float | None,
        moisture: float | None,
    ) -> bool:
        return await self.players.set_vitals(
            userid,
            health=health,
            hunger=hunger,
            sanity=sanity,
            temperature=temperature,
            moisture=moisture,
        )

    async def kill_player(self, userid: str) -> bool:
        return await self.players.kill(userid)

    async def revive(self, userid: str) -> bool:
        return await self.players.revive(userid)

    async def despawn(self, userid: str) -> bool:
        return await self.players.despawn(userid)

    async def migrate(self, userid: str, shard_id: str, portal_id: int) -> bool:
        return await self.players.migrate(userid, shard_id, portal_id)

    async def teleport(
        self,
        userid: str,
        x: float,
        y: float,
        z: float,
    ) -> bool:
        return await self.players.teleport(userid, x, y, z)

    async def give(self, userid: str, item: str, count: int) -> int:
        return await self.players.give(userid, item, count)

    async def remove(self, userid: str, item: str, count: int) -> int:
        return await self.players.remove(userid, item, count)

    async def save_marker(self) -> int:
        return await self._uint("saveMarker")

    async def save(self, timeout: float = DEFAULT_OPERATION_TIMEOUT) -> SavedEvent:
        return await self._model("save", SavedEvent, mutation=True, timeout=timeout)

    async def wait_saved(
        self,
        after_sequence: int,
        snapshot: int | None,
        completion_timeout: float,
    ) -> SavedEvent:
        return await self._model(
            "waitSaved",
            SavedEvent,
            afterSequence=after_sequence,
            snapshot=_nullable(snapshot, _scalar),
            timeout=completion_timeout,
        )

    async def generation_marker(self) -> int:
        return await self._uint("generationMarker")

    async def wait_generation(
        self, after_generation: int, completion_timeout: float
    ) -> int:
        return await self._uint(
            "waitGeneration",
            afterGeneration=after_generation,
            timeout=completion_timeout,
        )

    async def announce(self, message: str) -> None:
        await self._call("announce", mutation=True, message=message)

    async def reset(self, completion_timeout: float) -> None:
        await self._call("reset", mutation=True, timeout=completion_timeout)

    async def rollback(self, count: int, completion_timeout: float) -> None:
        await self._call(
            "rollback", mutation=True, count=count, timeout=completion_timeout
        )

    async def regenerate(self, completion_timeout: float) -> None:
        await self._call("regenerate", mutation=True, timeout=completion_timeout)

    async def is_whitelisted(self, userid: str) -> bool:
        return await self._bool("isWhitelisted", userid=userid)

    async def whitelist(self, userid: str) -> bool:
        return await self._bool("whitelist", mutation=True, userid=userid)

    async def unwhitelist(self, userid: str) -> bool:
        return await self._bool("unwhitelist", mutation=True, userid=userid)

    async def aclose(self) -> None:
        if self._closed:
            return
        self._closed = True
        for task in self._pumps:
            task.cancel()
        try:
            async with asyncio.timeout(_CLOSE_TIMEOUT):
                await asyncio.gather(*self._pumps, return_exceptions=True)
                await asyncio.gather(
                    *(self._close_subscription(item) for item in self._subscriptions)
                )
        except TimeoutError:
            logger.warning("remote agent cleanup timed out: {shard}", shard=self.name)
        finally:
            self.logs.close()
            self.lifecycle.close()
            self.game_events.close()

    @staticmethod
    async def _close_subscription(subscription: RemoteSubscription[Any]) -> None:
        with suppress(Exception):
            async with asyncio.timeout(_CLOSE_TIMEOUT):
                await subscription.close()

    async def _release_subscription(
        self, subscription: RemoteSubscription[Any]
    ) -> None:
        with suppress(ValueError):
            self._subscriptions.remove(subscription)
        await self._close_subscription(subscription)

    async def _call(
        self,
        method: str,
        *,
        mutation: bool = False,
        decode: Callable[[Any], Any] | None = None,
        **arguments: object,
    ) -> Any:
        if self._closed:
            msg = "remote shard agent is closed"
            raise DisconnectedError(msg)
        value = await self._invoke(
            self.capability,
            method,
            mutation=mutation,
            **arguments,
        )
        return value if decode is None else decode(value)

    async def _model[ModelT: BaseModel](
        self,
        method: str,
        model: type[ModelT],
        *,
        mutation: bool = False,
        **arguments: object,
    ) -> ModelT:
        return await self._call(
            method,
            mutation=mutation,
            decode=lambda value: decode_model(model, value),
            **arguments,
        )

    async def _bool(
        self, method: str, *, mutation: bool = False, **arguments: object
    ) -> bool:
        return await self._call(
            method,
            mutation=mutation,
            decode=lambda value: bool(value.value),
            **arguments,
        )

    async def _uint(
        self, method: str, *, mutation: bool = False, **arguments: object
    ) -> int:
        return await self._call(
            method,
            mutation=mutation,
            decode=lambda value: int(value.value),
            **arguments,
        )

    async def _subscribe[RecordT: BaseModel](
        self,
        method: str,
        model: type[RecordT],
    ) -> RemoteSubscription[RecordT]:
        return RemoteSubscription(
            await self._call(method),
            lambda value: decode_model(model, value),
        )

    async def _pump[RecordT: BaseModel](
        self,
        subscription: RemoteSubscription[RecordT],
        subscribe: str,
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
            failure: Exception | None = None
            try:
                await self._relay_stream(current, target, model)
            except asyncio.CancelledError:
                raise
            except Exception as error:
                failure = error
            finally:
                await self._release_subscription(current)
                current = None
            if not self._closed:
                logger.warning(
                    "remote observation stream ended; reconnecting: "
                    "{shard}: {stream}: {kind}",
                    shard=self.name,
                    stream=model.__name__,
                    kind="closed" if failure is None else type(failure).__name__,
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


class WorkerRegistryServant(_Responder, schema.WorkerRegistry.Server):
    def __init__(self, controller: ClusterController) -> None:
        super().__init__()
        self.controller = controller
        self.remote: RemoteAgent | None = None
        self._closed = False
        self._register_lock = asyncio.Lock()

    async def register(
        self,
        schemaFingerprint: str,
        agent: Any,
        _context: Any,
    ) -> None:
        if schemaFingerprint != SCHEMA_FINGERPRINT:
            _context.results.result = failure(
                ErrorInfo(
                    ErrorCode.INCOMPATIBLE_SCHEMA,
                    ULID(),
                    "RPC schema is incompatible",
                )
            )
            return

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
