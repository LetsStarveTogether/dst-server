from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Annotated, Any, ClassVar, Literal, Self

import orjson
from pydantic import Field, JsonValue, TypeAdapter, model_validator

from dst_server.announcements import (
    DEFAULT_DELAY,
    DEFAULT_INTERVAL,
    MOD_UPDATE_NOTICE,
    RESTART_NOTICE,
    SHUTDOWN_NOTICE,
    Countdown,
)
from dst_server.configuration.models import ClusterConfig
from dst_server.events.server import SavedEvent
from dst_server.json_codec import validate_json_structure
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
)
from dst_server.models.cluster import (
    ClusterSaveResult,
    ClusterStatus,
    LocatedPlayer,
    ObservationCursor,
    ShardResult,
    ShardRuntimeStatus,
)
from dst_server.models.console import ConsoleResult
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
    """Read the runtime status of this shard."""

    method = "status"


class ClusterStatusQuery(Request[ClusterStatus]):
    """Read room status, including all configured shards."""

    method = "status"


class Start(Request[None]):
    """Start the room or shard and wait for lifecycle completion."""

    method = "start"
    timeout: Timeout = DEFAULT_LIFECYCLE_TIMEOUT


class Stop(Request[None]):
    """Stop the room or shard gracefully."""

    method = "stop"
    timeout: Timeout = DEFAULT_STOP_TIMEOUT + DEFAULT_DELAY
    notice: Countdown | None = SHUTDOWN_NOTICE


class Restart(Request[None]):
    """Stop and start the room or shard."""

    method = "restart"
    timeout: Timeout = DEFAULT_LIFECYCLE_TIMEOUT
    notice: Countdown | None = RESTART_NOTICE


class Kill(Request[None]):
    """Terminate the room or shard immediately."""

    method = "kill"
    timeout: Timeout = DEFAULT_STOP_TIMEOUT


class UpdateMods(Request[None]):
    """Update the MOD files for the room."""

    method = "update_mods"
    timeout: Timeout = DEFAULT_LIFECYCLE_TIMEOUT
    restart: bool = False
    notice: Countdown | None = MOD_UPDATE_NOTICE


class ReadConfiguration(Request[ClusterConfig]):
    """Read the native room configuration."""

    method = "read_configuration"


class Execute(Request[str]):
    """Execute Lua source and return its text response."""

    method = "execute"
    source: NonEmptyText


class ExecuteJson(Request[JsonValue]):
    """Execute Lua source and decode its JSON response."""

    method = "execute_json"
    source: NonEmptyText


class Evaluate(Request[ConsoleResult]):
    """Evaluate Lua once, returning output, values and compile/runtime errors."""

    method = "evaluate"
    source: NonEmptyText


class ExecuteAll(Request[tuple[ShardResult[str], ...]]):
    """Execute Lua source on every shard and return individual results."""

    method = "execute_all"
    source: NonEmptyText


class Announce(Request[None]):
    """Broadcast an announcement to players in the room."""

    method = "announce"
    message: NonEmptyText
    count: Positive = 1
    interval: Timeout = DEFAULT_INTERVAL


class Save(Request[SavedEvent]):
    """Save this shard and wait for its save event."""

    method = "save"
    timeout: Timeout = DEFAULT_SAVE_TIMEOUT


class ClusterSave(Request[ClusterSaveResult]):
    """Save every shard and return their save results."""

    method = "save"
    timeout: Timeout = DEFAULT_SAVE_TIMEOUT


class Pause(Request[bool]):
    """Set whether the game is paused on this shard."""

    method = "pause"
    paused: bool


class ClusterPause(Request[tuple[ShardResult[bool], ...]]):
    """Set whether the game is paused on every shard."""

    method = "pause"
    paused: bool


class Reset(Request[None]):
    """Reload the world from its current save."""

    method = "reset"
    timeout: Timeout = DEFAULT_RELOAD_TIMEOUT


class Rollback(Request[None]):
    """Roll back the world by a number of snapshots."""

    method = "rollback"
    count: Natural = 1
    timeout: Timeout = DEFAULT_RELOAD_TIMEOUT


class Regenerate(Request[None]):
    """Generate a new world, optionally requiring an empty current session."""

    method = "regenerate"
    timeout: Timeout = DEFAULT_RELOAD_TIMEOUT
    expected_session_id: Identifier | None = None
    require_empty: bool | None = None


class RegenerateShard(Request[None]):
    """Generate a new world for this shard."""

    method = "regenerate_shard"
    preserve_settings: bool = True
    timeout: Timeout = DEFAULT_RELOAD_TIMEOUT


class Snapshots(Request[SnapshotCatalog]):
    """List available snapshots, optionally before a snapshot number."""

    method = "list_snapshots"
    limit: Annotated[int, Field(ge=1, le=100)] = 100
    before: Natural | None = None


class RollbackToDay(Request[Snapshot]):
    """Roll back the room to a snapshot for the requested day."""

    method = "rollback_to_day"
    day: Positive
    timeout: Timeout = DEFAULT_RELOAD_TIMEOUT


class RollbackToSnapshot(Request[None]):
    method = "rollback_to_snapshot"
    session_id: Identifier
    snapshot_id: Positive
    timeout: Timeout = DEFAULT_RELOAD_TIMEOUT


class Health(Request[DriverHealth]):
    """Read game command transport health."""

    method = "health"


class Room(Request[RoomInfo]):
    """Read game room details."""

    method = "room"


class World(Request[WorldInfo]):
    """Read current world details."""

    method = "world"


class Runtime(Request[RuntimeInfo]):
    """Read game runtime details."""

    method = "runtime"


class Mods(Request[tuple[Mod, ...]]):
    """List the MODs loaded by the game."""

    method = "mods"


class ConnectedShards(Request[tuple[ShardStatus, ...]]):
    """List the shards connected to this game instance."""

    method = "connected_shards"


class ListPlayers(Request[tuple[Player, ...]]):
    """List players on this shard."""

    method = "list_players"


class LocatePlayers(Request[tuple[LocatedPlayer, ...]]):
    """List players across the room with their shard locations."""

    method = "list_players"


class _PlayerRequest[T](Request[T]):
    userid: Identifier


class GetPlayer(_PlayerRequest[Player | None]):
    """Read one player on this shard, or null when absent."""

    method = "get_player"


class LocatePlayer(_PlayerRequest[LocatedPlayer | None]):
    """Locate one player across the room, or null when absent."""

    method = "get_player"


class Inventory(_PlayerRequest[PlayerInventory | None]):
    """Read the inventory of a player on this shard."""

    method = "inventory"


class Kick(_PlayerRequest[None]):
    """Disconnect a player from this shard."""

    method = "kick"


class Ban(_PlayerRequest[None]):
    """Ban a player, optionally for a limited number of seconds."""

    method = "ban"
    seconds: Positive | None = None


class Blocklist(Request[tuple[str, ...]]):
    """List blocked player identifiers."""

    method = "blocklist"


class IsBlocked(_PlayerRequest[bool]):
    """Check whether a player is blocked."""

    method = "is_blocked"


class Unban(_PlayerRequest[bool]):
    """Remove a player from the blocklist."""

    method = "unban"


class IsAdmin(_PlayerRequest[bool | None]):
    """Check whether a connected player is an administrator."""

    method = "is_admin"


class SetVitals(_PlayerRequest[bool]):
    """Change one or more player vitals."""

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
    """Kill a player character."""

    method = "kill_player"


class Revive(_PlayerRequest[bool]):
    """Revive a player character."""

    method = "revive"


class Despawn(_PlayerRequest[bool]):
    """Despawn a player character."""

    method = "despawn"


class Migrate(_PlayerRequest[bool]):
    """Move a player to another connected shard through a portal."""

    method = "migrate"
    shard_id: Identifier
    portal_id: Positive = 1


class Teleport(_PlayerRequest[bool]):
    """Move a player to the given world coordinates."""

    method = "teleport"
    x: FiniteFloat
    y: FiniteFloat
    z: FiniteFloat


class Give(_PlayerRequest[int]):
    """Give a player a number of items."""

    method = "give"
    item: NonEmptyText
    count: Annotated[int, Field(ge=1, le=64)] = 1


class Remove(_PlayerRequest[int]):
    """Remove a number of matching items from a player."""

    method = "remove"
    item: NonEmptyText
    count: Positive = 1


class IsWhitelisted(_PlayerRequest[bool]):
    """Check whether a player is on the room whitelist."""

    method = "is_whitelisted"


class Whitelist(_PlayerRequest[bool]):
    """Add a player to the room whitelist."""

    method = "whitelist"


class Unwhitelist(_PlayerRequest[bool]):
    """Remove a player from the room whitelist."""

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


@dataclass(frozen=True, slots=True)
class Operation:
    request: type[Request[Any]]
    result_type: Any
    mutation: bool
    scopes: tuple[Scope, ...]
    game: Literal["request", "reload"] | None = None
    response: TypeAdapter[Any] = field(init=False)

    def __post_init__(self) -> None:
        object.__setattr__(self, "response", TypeAdapter(self.result_type))


class MethodDescription(FrozenModel):
    name: NonEmptyText
    scope: Literal["cluster", "shard"]
    description: str
    arguments_schema: dict[str, Any]
    result_schema: dict[str, Any]
    default_timeout: Timeout
    mutation: bool


METHOD_DESCRIPTIONS = TypeAdapter(tuple[MethodDescription, ...])


_ALL: tuple[Scope, ...] = ("cluster", "shard", "agent")
_SHARD: tuple[Scope, ...] = ("shard", "agent")
_MASTER: tuple[Scope, ...] = ("cluster", "agent")
_CLUSTER: tuple[Scope, ...] = ("cluster",)
_AGENT: tuple[Scope, ...] = ("agent",)
OPERATIONS = (
    Operation(Status, ShardRuntimeStatus, False, _SHARD),
    Operation(ClusterStatusQuery, ClusterStatus, False, _CLUSTER),
    Operation(Start, None, True, _ALL),
    Operation(Stop, None, True, _ALL),
    Operation(Restart, None, True, _ALL),
    Operation(Kill, None, True, _ALL),
    Operation(UpdateMods, None, True, _CLUSTER),
    Operation(ReadConfiguration, ClusterConfig, False, _CLUSTER),
    Operation(Execute, str, True, _SHARD),
    Operation(ExecuteJson, JsonValue, True, _SHARD, game="request"),
    Operation(Evaluate, ConsoleResult, True, _SHARD, game="request"),
    Operation(ExecuteAll, tuple[ShardResult[str], ...], True, _CLUSTER),
    Operation(Announce, None, True, _MASTER, game="request"),
    Operation(Save, SavedEvent, True, _SHARD),
    Operation(ClusterSave, ClusterSaveResult, True, _CLUSTER),
    Operation(Pause, bool, True, _SHARD, game="request"),
    Operation(ClusterPause, tuple[ShardResult[bool], ...], True, _CLUSTER),
    Operation(Reset, None, True, _MASTER, game="reload"),
    Operation(Rollback, None, True, _MASTER, game="reload"),
    Operation(Regenerate, None, True, _MASTER, game="reload"),
    Operation(RegenerateShard, None, True, _SHARD, game="reload"),
    Operation(Snapshots, SnapshotCatalog, False, _ALL, game="request"),
    Operation(RollbackToDay, Snapshot, True, _CLUSTER),
    Operation(RollbackToSnapshot, None, True, _AGENT, game="reload"),
    Operation(Health, DriverHealth, False, _SHARD, game="request"),
    Operation(Room, RoomInfo, False, _SHARD, game="request"),
    Operation(World, WorldInfo, False, _SHARD, game="request"),
    Operation(Runtime, RuntimeInfo, False, _SHARD, game="request"),
    Operation(Mods, tuple[Mod, ...], False, _SHARD, game="request"),
    Operation(ConnectedShards, tuple[ShardStatus, ...], False, _SHARD, game="request"),
    Operation(ListPlayers, tuple[Player, ...], False, _SHARD, game="request"),
    Operation(LocatePlayers, tuple[LocatedPlayer, ...], False, _CLUSTER),
    Operation(GetPlayer, Player | None, False, _SHARD, game="request"),
    Operation(LocatePlayer, LocatedPlayer | None, False, _CLUSTER),
    Operation(Inventory, PlayerInventory | None, False, _SHARD, game="request"),
    Operation(Kick, None, True, _SHARD, game="request"),
    Operation(Ban, None, True, _SHARD, game="request"),
    Operation(Blocklist, tuple[Identifier, ...], False, _SHARD, game="request"),
    Operation(IsBlocked, bool, False, _SHARD, game="request"),
    Operation(Unban, bool, True, _SHARD, game="request"),
    Operation(IsAdmin, bool | None, False, _SHARD),
    Operation(SetVitals, bool, True, _SHARD, game="request"),
    Operation(KillPlayer, bool, True, _SHARD, game="request"),
    Operation(Revive, bool, True, _SHARD, game="request"),
    Operation(Despawn, bool, True, _SHARD, game="request"),
    Operation(Migrate, bool, True, _SHARD, game="request"),
    Operation(Teleport, bool, True, _SHARD, game="request"),
    Operation(Give, int, True, _SHARD, game="request"),
    Operation(Remove, int, True, _SHARD, game="request"),
    Operation(IsWhitelisted, bool, False, _MASTER, game="request"),
    Operation(Whitelist, bool, True, _MASTER, game="request"),
    Operation(Unwhitelist, bool, True, _MASTER, game="request"),
    Operation(Activate, None, True, _AGENT),
    Operation(SaveMarker, ObservationCursor, False, _AGENT),
    Operation(WaitSaved, SavedEvent, False, _AGENT),
)
_OPERATIONS = {
    (scope, spec.request.method): spec for spec in OPERATIONS for scope in spec.scopes
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


def describe_operations(scope: str) -> tuple[MethodDescription, ...]:
    """Describe the registered methods exposed by a public endpoint."""
    if scope not in {"cluster", "shard"}:
        msg = "method discovery is only available for cluster and shard endpoints"
        raise ValueError(msg)
    descriptions = []
    for (registered_scope, name), spec in sorted(_OPERATIONS.items()):
        if registered_scope != scope:
            continue
        arguments_schema = spec.request.model_json_schema()
        arguments_schema["properties"].pop("timeout", None)
        descriptions.append(
            MethodDescription(
                name=name,
                scope=scope,
                description=spec.request.__doc__ or "",
                arguments_schema=arguments_schema,
                result_schema=spec.response.json_schema(mode="serialization"),
                default_timeout=spec.request.model_fields["timeout"].default,
                mutation=spec.mutation,
            )
        )
    return tuple(descriptions)


class _Envelope(FrozenModel):
    method: NonEmptyText
    arguments: dict[str, JsonValue] = Field(default_factory=dict)
    timeout: Timeout | None = None


def encode_request(request: Request[Any]) -> bytes:
    request = type(request).model_validate(request)
    arguments = request.model_dump(
        mode="json", exclude={"timeout"}, exclude_unset=True, context={"secrets": True}
    )
    return encode_call(request.method, arguments, timeout=request.timeout)


def encode_call(
    method: str,
    arguments: Mapping[str, JsonValue] | None = None,
    *,
    timeout: float | None = None,
) -> bytes:
    envelope = _Envelope(
        method=method, arguments=dict(arguments or {}), timeout=timeout
    )
    if {"method", "timeout"}.intersection(envelope.arguments):
        msg = "command arguments contain reserved fields"
        raise ValueError(msg)
    return envelope.model_dump_json().encode()


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
    return spec.request.model_validate_json(orjson.dumps(arguments))
