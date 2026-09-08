import json
from dataclasses import dataclass
from typing import Annotated, Any, ClassVar, Literal, Self

from pydantic import Field, JsonValue, TypeAdapter, model_validator

from dst_server.configuration.models import ClusterConfig
from dst_server.events.server import SavedEvent
from dst_server.models import Inventory as PlayerInventory
from dst_server.models import Mod, Player, ShardStatus
from dst_server.models import Room as RoomInfo
from dst_server.models import Runtime as RuntimeInfo
from dst_server.models import World as WorldInfo
from dst_server.models.base import (
    FiniteFloat,
    FrozenModel,
    Identifier,
    Percent,
    RevalidatedFrozenModel,
    ULIDValue,
)
from dst_server.models.cluster import (
    ClusterSaveResult,
    ClusterStatus,
    ConfigurationRead,
    ConfigurationSnapshot,
    LocatedPlayer,
    ObservationCursor,
    ShardResult,
    ShardRuntimeStatus,
)
from dst_server.models.driver import DriverHealth
from dst_server.models.snapshot import Snapshot, SnapshotCatalog
from dst_server.timeouts import (
    DEFAULT_COMMAND_TIMEOUT,
    DEFAULT_LIFECYCLE_TIMEOUT,
    DEFAULT_RELOAD_TIMEOUT,
    DEFAULT_SAVE_TIMEOUT,
    DEFAULT_STOP_TIMEOUT,
    Timeout,
)

type Scope = Literal["cluster", "shard", "agent"]
type NonEmptyText = Annotated[str, Field(min_length=1)]
type Natural = Annotated[int, Field(ge=0, le=2**53 - 1)]
type Positive = Annotated[int, Field(ge=1, le=2**53 - 1)]


class Request[T](RevalidatedFrozenModel):
    method: ClassVar[str]
    timeout: Timeout = DEFAULT_COMMAND_TIMEOUT

    @property
    def arguments(self) -> dict[str, object]:
        return {
            name: getattr(self, name)
            for name in type(self).model_fields
            if name != "timeout"
        }


class Status(Request[ShardRuntimeStatus]):
    method = "status"


class ClusterStatusQuery(Request[ClusterStatus]):
    method = "status"


class Start(Request[None]):
    method = "start"
    timeout: Timeout = DEFAULT_LIFECYCLE_TIMEOUT


class Stop(Request[None]):
    method = "stop"
    timeout: Timeout = DEFAULT_STOP_TIMEOUT


class Restart(Request[None]):
    method = "restart"
    timeout: Timeout = DEFAULT_LIFECYCLE_TIMEOUT


class Kill(Request[None]):
    method = "kill"
    timeout: Timeout = DEFAULT_STOP_TIMEOUT


class UpdateMods(Request[None]):
    method = "update_mods"
    timeout: Timeout = DEFAULT_LIFECYCLE_TIMEOUT


class ReadConfiguration(Request[ConfigurationRead]):
    method = "read_configuration"


class SaveConfiguration(Request[ConfigurationSnapshot]):
    method = "save_configuration"
    expected_revision: ULIDValue
    configuration: ClusterConfig


class Execute(Request[str]):
    method = "execute"
    source: NonEmptyText


class ExecuteJson(Request[JsonValue]):
    method = "execute_json"
    source: NonEmptyText


class ExecuteAll(Request[tuple[ShardResult[str], ...]]):
    method = "execute_all"
    source: NonEmptyText


class Announce(Request[None]):
    method = "announce"
    message: NonEmptyText


class Save(Request[SavedEvent]):
    method = "save"
    timeout: Timeout = DEFAULT_SAVE_TIMEOUT


class ClusterSave(Request[ClusterSaveResult]):
    method = "save"
    timeout: Timeout = DEFAULT_SAVE_TIMEOUT


class Pause(Request[bool]):
    method = "pause"
    paused: bool


class ClusterPause(Request[tuple[ShardResult[bool], ...]]):
    method = "pause"
    paused: bool


class Reset(Request[None]):
    method = "reset"
    timeout: Timeout = DEFAULT_RELOAD_TIMEOUT


class Rollback(Request[None]):
    method = "rollback"
    count: Natural = 1
    timeout: Timeout = DEFAULT_RELOAD_TIMEOUT


class Regenerate(Request[None]):
    method = "regenerate"
    timeout: Timeout = DEFAULT_RELOAD_TIMEOUT


class RegenerateShard(Request[None]):
    method = "regenerate_shard"
    preserve_settings: bool = True
    timeout: Timeout = DEFAULT_RELOAD_TIMEOUT


class Snapshots(Request[SnapshotCatalog]):
    method = "list_snapshots"
    limit: Annotated[int, Field(ge=1, le=100)] = 100
    before: Natural | None = None


class RollbackToDay(Request[Snapshot]):
    method = "rollback_to_day"
    day: Positive
    timeout: Timeout = DEFAULT_RELOAD_TIMEOUT


class RollbackToSnapshot(Request[None]):
    method = "rollback_to_snapshot"
    session_id: Identifier
    snapshot_id: Positive
    timeout: Timeout = DEFAULT_RELOAD_TIMEOUT


class Health(Request[DriverHealth]):
    method = "health"


class Room(Request[RoomInfo]):
    method = "room"


class World(Request[WorldInfo]):
    method = "world"


class Runtime(Request[RuntimeInfo]):
    method = "runtime"


class Mods(Request[tuple[Mod, ...]]):
    method = "mods"


class ConnectedShards(Request[tuple[ShardStatus, ...]]):
    method = "connected_shards"


class ListPlayers(Request[tuple[Player, ...]]):
    method = "list_players"


class LocatePlayers(Request[tuple[LocatedPlayer, ...]]):
    method = "list_players"


class _PlayerRequest[T](Request[T]):
    userid: Identifier


class GetPlayer(_PlayerRequest[Player | None]):
    method = "get_player"


class LocatePlayer(_PlayerRequest[LocatedPlayer | None]):
    method = "get_player"


class Inventory(_PlayerRequest[PlayerInventory | None]):
    method = "inventory"


class Kick(_PlayerRequest[None]):
    method = "kick"


class Ban(_PlayerRequest[None]):
    method = "ban"
    seconds: Positive | None = None


class Blocklist(Request[tuple[str, ...]]):
    method = "blocklist"


class IsBlocked(_PlayerRequest[bool]):
    method = "is_blocked"


class Unban(_PlayerRequest[bool]):
    method = "unban"


class IsAdmin(_PlayerRequest[bool | None]):
    method = "is_admin"


class SetVitals(_PlayerRequest[bool]):
    method = "set_vitals"
    health: Percent | None = None
    hunger: Percent | None = None
    sanity: Percent | None = None
    temperature: FiniteFloat | None = None
    moisture: Percent | None = None

    @model_validator(mode="after")
    def _requires_change(self) -> Self:
        if all(
            getattr(self, name) is None
            for name in ("health", "hunger", "sanity", "temperature", "moisture")
        ):
            msg = "at least one player vital must be supplied"
            raise ValueError(msg)
        return self


class KillPlayer(_PlayerRequest[bool]):
    method = "kill_player"


class Revive(_PlayerRequest[bool]):
    method = "revive"


class Despawn(_PlayerRequest[bool]):
    method = "despawn"


class Migrate(_PlayerRequest[bool]):
    method = "migrate"
    shard_id: Identifier
    portal_id: Positive = 1


class Teleport(_PlayerRequest[bool]):
    method = "teleport"
    x: FiniteFloat
    y: FiniteFloat
    z: FiniteFloat


class Give(_PlayerRequest[int]):
    method = "give"
    item: NonEmptyText
    count: Annotated[int, Field(ge=1, le=64)] = 1


class Remove(_PlayerRequest[int]):
    method = "remove"
    item: NonEmptyText
    count: Positive = 1


class IsWhitelisted(_PlayerRequest[bool]):
    method = "is_whitelisted"


class Whitelist(_PlayerRequest[bool]):
    method = "whitelist"


class Unwhitelist(_PlayerRequest[bool]):
    method = "unwhitelist"


class Activate(Request[None]):
    method = "activate"


class SaveMarker(Request[ObservationCursor]):
    method = "save_marker"


class WaitSaved(Request[SavedEvent]):
    method = "wait_saved"
    cursor: ObservationCursor
    snapshot: Natural | None = None
    timeout: Timeout = DEFAULT_SAVE_TIMEOUT


class GenerationMarker(Request[ObservationCursor]):
    method = "generation_marker"


class WaitGeneration(Request[int]):
    method = "wait_generation"
    cursor: ObservationCursor
    timeout: Timeout = DEFAULT_RELOAD_TIMEOUT


@dataclass(frozen=True, slots=True)
class Operation:
    request: type[Request[Any]]
    response: TypeAdapter[Any]
    mutation: bool
    result_type: Any


_ALL: tuple[Scope, ...] = ("cluster", "shard", "agent")
_SHARD: tuple[Scope, ...] = ("shard", "agent")
_MASTER: tuple[Scope, ...] = ("cluster", "agent")
_CLUSTER: tuple[Scope, ...] = ("cluster",)
_AGENT: tuple[Scope, ...] = ("agent",)
_DECLARATIONS: tuple[
    tuple[type[Request[Any]], object, bool, tuple[Scope, ...]], ...
] = (
    (Status, ShardRuntimeStatus, False, _SHARD),
    (ClusterStatusQuery, ClusterStatus, False, _CLUSTER),
    (Start, None, True, _ALL),
    (Stop, None, True, _ALL),
    (Restart, None, True, _ALL),
    (Kill, None, True, _ALL),
    (UpdateMods, None, True, _CLUSTER),
    (ReadConfiguration, ConfigurationRead, False, _CLUSTER),
    (SaveConfiguration, ConfigurationSnapshot, True, _CLUSTER),
    (Execute, str, True, _SHARD),
    (ExecuteJson, JsonValue, True, _SHARD),
    (ExecuteAll, tuple[ShardResult[str], ...], True, _CLUSTER),
    (Announce, None, True, _MASTER),
    (Save, SavedEvent, True, _SHARD),
    (ClusterSave, ClusterSaveResult, True, _CLUSTER),
    (Pause, bool, True, _SHARD),
    (ClusterPause, tuple[ShardResult[bool], ...], True, _CLUSTER),
    (Reset, None, True, _MASTER),
    (Rollback, None, True, _MASTER),
    (Regenerate, None, True, _MASTER),
    (RegenerateShard, None, True, _SHARD),
    (Snapshots, SnapshotCatalog, False, _ALL),
    (RollbackToDay, Snapshot, True, _CLUSTER),
    (RollbackToSnapshot, None, True, _AGENT),
    (Health, DriverHealth, False, _SHARD),
    (Room, RoomInfo, False, _SHARD),
    (World, WorldInfo, False, _SHARD),
    (Runtime, RuntimeInfo, False, _SHARD),
    (Mods, tuple[Mod, ...], False, _SHARD),
    (ConnectedShards, tuple[ShardStatus, ...], False, _SHARD),
    (ListPlayers, tuple[Player, ...], False, _SHARD),
    (LocatePlayers, tuple[LocatedPlayer, ...], False, _CLUSTER),
    (GetPlayer, Player | None, False, _SHARD),
    (LocatePlayer, LocatedPlayer | None, False, _CLUSTER),
    (Inventory, PlayerInventory | None, False, _SHARD),
    (Kick, None, True, _SHARD),
    (Ban, None, True, _SHARD),
    (Blocklist, tuple[Identifier, ...], False, _SHARD),
    (IsBlocked, bool, False, _SHARD),
    (Unban, bool, True, _SHARD),
    (IsAdmin, bool | None, False, _SHARD),
    (SetVitals, bool, True, _SHARD),
    (KillPlayer, bool, True, _SHARD),
    (Revive, bool, True, _SHARD),
    (Despawn, bool, True, _SHARD),
    (Migrate, bool, True, _SHARD),
    (Teleport, bool, True, _SHARD),
    (Give, int, True, _SHARD),
    (Remove, int, True, _SHARD),
    (IsWhitelisted, bool, False, _MASTER),
    (Whitelist, bool, True, _MASTER),
    (Unwhitelist, bool, True, _MASTER),
    (Activate, None, True, _AGENT),
    (SaveMarker, ObservationCursor, False, _AGENT),
    (WaitSaved, SavedEvent, False, _AGENT),
    (GenerationMarker, ObservationCursor, False, _AGENT),
    (WaitGeneration, int, False, _AGENT),
)
_OPERATIONS = {
    (scope, request.method): Operation(request, TypeAdapter(result), mutation, result)
    for request, result, mutation, scopes in _DECLARATIONS
    for scope in scopes
}


def operation(scope: str, request: Request[Any] | str) -> Operation:
    method = request if isinstance(request, str) else request.method
    spec = _OPERATIONS.get((scope, method))
    if spec is None or (
        not isinstance(request, str) and type(request) is not spec.request
    ):
        msg = f"command {method!r} is not available to {scope!r}"
        raise ValueError(msg)
    if not isinstance(request, str):
        spec.request.model_validate(request)
    return spec


class _Envelope(FrozenModel):
    method: NonEmptyText
    arguments: dict[str, JsonValue] = Field(default_factory=dict)
    timeout: Timeout | None = None


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            msg = f"duplicate JSON object key: {key!r}"
            raise ValueError(msg)
        result[key] = value
    return result


def validate_json_structure(payload: bytes) -> None:
    def invalid_constant(value: str) -> None:
        msg = f"invalid JSON constant: {value}"
        raise ValueError(msg)

    json.loads(
        payload, object_pairs_hook=_unique_object, parse_constant=invalid_constant
    )


def encode_request(request: Request[Any]) -> bytes:
    request = type(request).model_validate(request)
    arguments = request.model_dump(
        mode="json", exclude={"timeout"}, exclude_unset=True, context={"secrets": True}
    )
    return (
        _Envelope(method=request.method, arguments=arguments, timeout=request.timeout)
        .model_dump_json()
        .encode()
    )


def parse_request(payload: bytes, *, scope: Scope) -> Request[Any]:
    validate_json_structure(payload)
    envelope = _Envelope.model_validate_json(payload)
    spec = operation(scope, envelope.method)
    if {"method", "timeout"}.intersection(envelope.arguments):
        msg = "command arguments contain reserved fields"
        raise ValueError(msg)
    arguments = envelope.arguments
    if envelope.timeout is not None:
        arguments |= {"timeout": envelope.timeout}
    return spec.request.model_validate_json(json.dumps(arguments, allow_nan=False))
