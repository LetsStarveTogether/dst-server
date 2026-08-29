# ruff: file-ignore[async-function-with-timeout, private-member-access]
import asyncio
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from importlib import import_module
from os import PathLike, fspath
from typing import Any, Self

from pydantic import JsonValue
from ulid import ULID

from dst_server.cluster.config import ClusterConfig
from dst_server.events.server import SavedEvent
from dst_server.game.rpc import DriverHealth
from dst_server.models import Inventory, Mod, Player, Room, Runtime, ShardStatus, World

from .codec import decode_json_value, decode_model, encode_model
from .errors import (
    DisconnectedError,
    ErrorCode,
    ErrorInfo,
    IndeterminateError,
    RemoteError,
    unwrap_outcome,
)
from .models import (
    ClusterStatus,
    GameEventRecord,
    LifecycleRecord,
    LocatedPlayer,
    LogRecord,
    ShardRuntimeStatus,
)
from .schema import SCHEMA_FINGERPRINT, load_schema

DEFAULT_OPERATION_TIMEOUT = 30.0
DEFAULT_BATCH_SIZE = 256
capnp: Any = import_module("capnp")


@asynccontextmanager
async def rpc_runtime() -> AsyncIterator[None]:
    async with capnp.kj_loop():
        yield


def _unit(_: Any) -> None:
    return None


def _scalar(value: Any) -> Any:
    return value.value


def _nullable[ValueT](
    value: Any,
    decode: Callable[[Any], ValueT],
) -> ValueT | None:
    selected = value.which()
    if selected == "none":
        return None
    if selected != "value":
        msg = f"invalid nullable RPC member: {selected}"
        raise ValueError(msg)
    return decode(value.value)


def _nullable_scalar(value: object | None) -> dict[str, object]:
    return {"none": None} if value is None else {"value": {"value": value}}


@dataclass(frozen=True, slots=True)
class ConfigurationSnapshot:
    revision: ULID
    configuration: ClusterConfig


@dataclass(frozen=True, slots=True)
class InvalidConfiguration:
    revision: ULID
    fields: tuple[tuple[str, ...], ...]


type ConfigurationRead = ConfigurationSnapshot | InvalidConfiguration


def _configuration_snapshot(value: Any) -> ConfigurationSnapshot:
    return ConfigurationSnapshot(
        revision=ULID.from_str(str(value.revision)),
        configuration=decode_model(ClusterConfig, value.configuration),
    )


@dataclass(frozen=True, slots=True)
class ShardResult[ValueT]:
    shard: str
    value: ValueT | None = None
    error: ErrorInfo | None = None


@dataclass(frozen=True, slots=True)
class ClusterSaveResult:
    snapshot: int | None
    shards: tuple[tuple[str, SavedEvent], ...]


def _nested_result[ValueT](
    value: Any,
    decode: Callable[[Any], ValueT],
) -> tuple[ValueT | None, ErrorInfo | None]:
    selected = value.which()
    if selected == "error":
        return None, ErrorInfo.from_wire(value.error)
    if selected == "value":
        return decode(value.value), None
    msg = f"invalid nested RPC outcome member: {selected}"
    raise ValueError(msg)


class Subscription[RecordT]:
    def __init__(
        self,
        capability: Any,
        decode: Callable[[Any], RecordT],
    ) -> None:
        self._capability = capability
        self._decode = decode
        self.closed = False

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *_: object) -> None:
        await self.close()

    async def next(self, max_items: int = DEFAULT_BATCH_SIZE) -> tuple[RecordT, ...]:
        if self.closed:
            return ()
        try:
            response = await self._capability.next(maxItems=max_items)
        except capnp.KjException as error:
            self.closed = True
            raise DisconnectedError(str(error)) from error
        batch = response.batch
        selected = batch.which()
        if selected == "items":
            return tuple(self._decode(item) for item in batch.items)
        if selected == "closed":
            self.closed = True
            return ()
        if selected == "error":
            error = ErrorInfo.from_wire(batch.error)
            if error.code is not ErrorCode.OVERFLOW:
                self.closed = True
            raise RemoteError(error)
        self.closed = True
        msg = f"invalid RPC batch member: {selected}"
        raise ValueError(msg)

    async def close(self) -> None:
        if self.closed:
            return
        try:
            response = await self._capability.close()
            _unit(unwrap_outcome(response.result))
        except capnp.KjException as error:
            raise DisconnectedError(str(error)) from error
        finally:
            self.closed = True


class _Remote:
    async def _invoke(
        self,
        capability: Any,
        method: str,
        *,
        mutation: bool = False,
        **arguments: object,
    ) -> Any:
        try:
            response = await getattr(capability, method)(**arguments)
        except asyncio.CancelledError as error:
            if mutation:
                raise IndeterminateError from error
            raise
        except capnp.KjException as error:
            if mutation:
                raise IndeterminateError from error
            raise DisconnectedError(str(error)) from error
        return unwrap_outcome(response.result)


class ClusterClient(_Remote):  # ruff: ignore[too-many-public-methods]
    def __init__(self, stream: Any, client: Any, capability: Any) -> None:
        self._stream = stream
        self._client = client
        self._capability = capability
        self._shards: dict[str, ShardClient] = {}
        self._closed = False

    @classmethod
    async def connect(cls, path: str | PathLike[str]) -> Self:
        address = fspath(path)
        stream = await capnp.AsyncIoStream.create_unix_connection(address)
        client = capnp.TwoPartyClient(stream)
        try:
            schema = load_schema()
            bootstrap = client.bootstrap().cast_as(schema.Bootstrap)
            response = await bootstrap.connect(schemaFingerprint=SCHEMA_FINGERPRINT)
            capability = unwrap_outcome(response.result)
        except BaseException:
            client.close()
            stream.close()
            raise
        return cls(stream, client, capability)

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *_: object) -> None:
        self.close()

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._client.close()
        self._stream.close()

    async def _call(
        self,
        method: str,
        *,
        mutation: bool = False,
        **arguments: object,
    ) -> Any:
        if self._closed:
            msg = "ClusterClient is closed"
            raise DisconnectedError(msg)
        return await self._invoke(
            self._capability,
            method,
            mutation=mutation,
            **arguments,
        )

    async def _get_shard(self, shard_name: str) -> Any:
        return await self._call("shard", shardName=shard_name)

    def shard(self, shard_name: str) -> ShardClient:
        return self._shards.setdefault(shard_name, ShardClient(self, shard_name))

    async def status(self) -> ClusterStatus:
        return decode_model(ClusterStatus, await self._call("status"))

    async def start(self) -> None:
        _unit(await self._call("start", mutation=True))

    async def stop(self) -> None:
        _unit(await self._call("stop", mutation=True))

    async def restart(self) -> None:
        _unit(await self._call("restart", mutation=True))

    async def kill(self) -> None:
        _unit(await self._call("kill", mutation=True))

    async def update_mods(self) -> None:
        _unit(await self._call("updateMods", mutation=True))

    async def read_configuration(self) -> ConfigurationRead:
        value = await self._call("readConfiguration")
        selected = value.which()
        if selected == "valid":
            return _configuration_snapshot(value.valid)
        if selected == "invalid":
            return InvalidConfiguration(
                revision=ULID.from_str(str(value.invalid.revision)),
                fields=tuple(
                    tuple(map(str, field.components)) for field in value.invalid.fields
                ),
            )
        msg = f"invalid configuration read member: {selected}"
        raise ValueError(msg)

    async def save_configuration(
        self,
        expected_revision: ULID,
        configuration: ClusterConfig,
    ) -> ConfigurationSnapshot:
        value = await self._call(
            "saveConfiguration",
            mutation=True,
            expectedRevision=str(expected_revision),
            configuration=encode_model(configuration),
        )
        return _configuration_snapshot(value)

    async def execute_all(
        self,
        source: str,
        timeout: float = DEFAULT_OPERATION_TIMEOUT,
    ) -> tuple[ShardResult[str], ...]:
        values = await self._call(
            "executeAll", mutation=True, source=source, timeout=timeout
        )
        result = []
        for item in values:
            value, error = _nested_result(item.result, lambda raw: str(_scalar(raw)))
            result.append(ShardResult(str(item.shard), value, error))
        return tuple(result)

    async def announce(self, message: str) -> None:
        _unit(await self._call("announce", mutation=True, message=message))

    async def save(
        self, timeout: float = DEFAULT_OPERATION_TIMEOUT
    ) -> ClusterSaveResult:
        value = await self._call("save", mutation=True, timeout=timeout)
        snapshot = _nullable(value.snapshot, lambda raw: int(_scalar(raw)))
        shards = tuple(
            (str(item.shard), decode_model(SavedEvent, item.event))
            for item in value.shards
        )
        return ClusterSaveResult(snapshot, shards)

    async def pause(self, paused: bool) -> tuple[ShardResult[bool], ...]:
        values = await self._call("pause", mutation=True, paused=paused)
        result = []
        for item in values:
            value, error = _nested_result(item.result, lambda raw: bool(_scalar(raw)))
            result.append(ShardResult(str(item.shard), value, error))
        return tuple(result)

    async def reset(self, timeout: float = DEFAULT_OPERATION_TIMEOUT) -> None:
        _unit(await self._call("reset", mutation=True, timeout=timeout))

    async def rollback(
        self,
        count: int = 1,
        *,
        timeout: float = DEFAULT_OPERATION_TIMEOUT,
    ) -> None:
        _unit(
            await self._call(
                "rollback",
                mutation=True,
                count=count,
                timeout=timeout,
            )
        )

    async def regenerate(self, timeout: float = DEFAULT_OPERATION_TIMEOUT) -> None:
        _unit(await self._call("regenerate", mutation=True, timeout=timeout))

    async def list_players(self) -> tuple[LocatedPlayer, ...]:
        values = await self._call("listPlayers")
        return tuple(decode_model(LocatedPlayer, item) for item in values)

    async def get_player(self, userid: str) -> LocatedPlayer | None:
        value = await self._call("getPlayer", userid=userid)
        return _nullable(value, lambda raw: decode_model(LocatedPlayer, raw))

    async def is_whitelisted(self, userid: str) -> bool:
        return bool(_scalar(await self._call("isWhitelisted", userid=userid)))

    async def whitelist(self, userid: str) -> bool:
        return bool(
            _scalar(await self._call("whitelist", mutation=True, userid=userid))
        )

    async def unwhitelist(self, userid: str) -> bool:
        return bool(
            _scalar(await self._call("unwhitelist", mutation=True, userid=userid))
        )

    async def subscribe_logs(self) -> Subscription[LogRecord]:
        capability = await self._call("subscribeLogs")
        return Subscription(capability, lambda raw: decode_model(LogRecord, raw))

    async def subscribe_lifecycle(self) -> Subscription[LifecycleRecord]:
        capability = await self._call("subscribeLifecycle")
        return Subscription(capability, lambda raw: decode_model(LifecycleRecord, raw))

    async def subscribe_events(self) -> Subscription[GameEventRecord]:
        capability = await self._call("subscribeEvents")
        return Subscription(capability, lambda raw: decode_model(GameEventRecord, raw))


class _ShardClient(_Remote):
    async def _call(
        self,
        method: str,
        *,
        mutation: bool = False,
        **arguments: object,
    ) -> Any:
        raise NotImplementedError

    async def status(self) -> ShardRuntimeStatus:
        return decode_model(ShardRuntimeStatus, await self._call("status"))

    async def start(self) -> None:
        _unit(await self._call("start", mutation=True))

    async def stop(self) -> None:
        _unit(await self._call("stop", mutation=True))

    async def restart(self) -> None:
        _unit(await self._call("restart", mutation=True))

    async def kill(self) -> None:
        _unit(await self._call("kill", mutation=True))

    async def execute(
        self,
        source: str,
        timeout: float = DEFAULT_OPERATION_TIMEOUT,
    ) -> str:
        return str(
            _scalar(
                await self._call(
                    "execute", mutation=True, source=source, timeout=timeout
                )
            )
        )

    async def save(self, timeout: float = DEFAULT_OPERATION_TIMEOUT) -> SavedEvent:
        return decode_model(
            SavedEvent,
            await self._call("save", mutation=True, timeout=timeout),
        )

    async def execute_json(self, source: str) -> JsonValue:
        return decode_json_value(
            await self._call("executeJson", mutation=True, source=source)
        )

    async def health(self) -> DriverHealth:
        return decode_model(DriverHealth, await self._call("health"))

    async def room(self) -> Room:
        return decode_model(Room, await self._call("room"))

    async def world(self) -> World:
        return decode_model(World, await self._call("world"))

    async def runtime(self) -> Runtime:
        return decode_model(Runtime, await self._call("runtime"))

    async def mods(self) -> tuple[Mod, ...]:
        values = await self._call("mods")
        return tuple(decode_model(Mod, item) for item in values)

    async def connected_shards(self) -> tuple[ShardStatus, ...]:
        values = await self._call("connectedShards")
        return tuple(decode_model(ShardStatus, item) for item in values)

    async def pause(self, paused: bool) -> bool:
        return bool(_scalar(await self._call("pause", mutation=True, paused=paused)))

    async def subscribe_logs(self) -> Subscription[LogRecord]:
        return Subscription(
            await self._call("subscribeLogs"),
            lambda raw: decode_model(LogRecord, raw),
        )

    async def subscribe_lifecycle(self) -> Subscription[LifecycleRecord]:
        return Subscription(
            await self._call("subscribeLifecycle"),
            lambda raw: decode_model(LifecycleRecord, raw),
        )

    async def subscribe_events(self) -> Subscription[GameEventRecord]:
        return Subscription(
            await self._call("subscribeEvents"),
            lambda raw: decode_model(GameEventRecord, raw),
        )


class ShardClient(_ShardClient):
    def __init__(self, cluster: ClusterClient, shard_name: str) -> None:
        self.cluster = cluster
        self.name = shard_name
        self.players = RemotePlayerClient(self)

    async def _call(
        self,
        method: str,
        *,
        mutation: bool = False,
        **arguments: object,
    ) -> Any:
        return await self._invoke(
            await self.cluster._get_shard(self.name),
            method,
            mutation=mutation,
            **arguments,
        )

    async def regenerate_shard(
        self,
        *,
        preserve_settings: bool = True,
        timeout: float = DEFAULT_OPERATION_TIMEOUT,
    ) -> None:
        _unit(
            await self._call(
                "regenerateShard",
                mutation=True,
                preserveSettings=preserve_settings,
                timeout=timeout,
            )
        )


class RemotePlayerClient:
    def __init__(self, shard: _ShardClient) -> None:
        self.shard = shard

    async def list(self) -> tuple[Player, ...]:
        values = await self.shard._call("listPlayers")
        return tuple(decode_model(Player, item) for item in values)

    async def get(self, userid: str) -> Player | None:
        value = await self.shard._call("getPlayer", userid=userid)
        return _nullable(value, lambda raw: decode_model(Player, raw))

    async def inventory(self, userid: str) -> Inventory | None:
        value = await self.shard._call("inventory", userid=userid)
        return _nullable(value, lambda raw: decode_model(Inventory, raw))

    async def kick(self, userid: str) -> None:
        _unit(await self.shard._call("kick", mutation=True, userid=userid))

    async def ban(self, userid: str, *, seconds: int | None = None) -> None:
        _unit(
            await self.shard._call(
                "ban",
                mutation=True,
                userid=userid,
                seconds=_nullable_scalar(seconds),
            )
        )

    async def blocklist(self) -> tuple[str, ...]:
        return tuple(await self.shard._call("blocklist"))

    async def is_blocked(self, userid: str) -> bool:
        return bool(_scalar(await self.shard._call("isBlocked", userid=userid)))

    async def unban(self, userid: str) -> bool:
        return bool(
            _scalar(await self.shard._call("unban", mutation=True, userid=userid))
        )

    async def is_admin(self, userid: str) -> bool | None:
        value = await self.shard._call("isAdmin", userid=userid)
        return _nullable(value, lambda raw: bool(_scalar(raw)))

    async def set_vitals(
        self,
        userid: str,
        *,
        health: float | None = None,
        hunger: float | None = None,
        sanity: float | None = None,
        temperature: float | None = None,
        moisture: float | None = None,
    ) -> bool:
        result = await self.shard._call(
            "setVitals",
            mutation=True,
            userid=userid,
            health=_nullable_scalar(health),
            hunger=_nullable_scalar(hunger),
            sanity=_nullable_scalar(sanity),
            temperature=_nullable_scalar(temperature),
            moisture=_nullable_scalar(moisture),
        )
        return bool(_scalar(result))

    async def kill(self, userid: str) -> bool:
        return bool(
            _scalar(await self.shard._call("killPlayer", mutation=True, userid=userid))
        )

    async def revive(self, userid: str) -> bool:
        return bool(
            _scalar(await self.shard._call("revive", mutation=True, userid=userid))
        )

    async def despawn(self, userid: str) -> bool:
        return bool(
            _scalar(await self.shard._call("despawn", mutation=True, userid=userid))
        )

    async def migrate(
        self,
        userid: str,
        shard_id: str,
        portal_id: int = 1,
    ) -> bool:
        result = await self.shard._call(
            "migrate",
            mutation=True,
            userid=userid,
            shardId=shard_id,
            portalId=portal_id,
        )
        return bool(_scalar(result))

    async def teleport(
        self,
        userid: str,
        x: float,
        y: float,
        z: float,
    ) -> bool:
        result = await self.shard._call(
            "teleport",
            mutation=True,
            userid=userid,
            x=x,
            y=y,
            z=z,
        )
        return bool(_scalar(result))

    async def give(self, userid: str, item: str, count: int = 1) -> int:
        result = await self.shard._call(
            "give",
            mutation=True,
            userid=userid,
            item=item,
            count=count,
        )
        return int(_scalar(result))

    async def remove(self, userid: str, item: str, count: int = 1) -> int:
        result = await self.shard._call(
            "remove",
            mutation=True,
            userid=userid,
            item=item,
            count=count,
        )
        return int(_scalar(result))
