from dataclasses import dataclass
from enum import StrEnum
from typing import Annotated, Literal

from pydantic import AwareDatetime, Field

from dst_server.errors import ErrorInfo
from dst_server.events import GameEvent
from dst_server.events.server import Event as LifecycleEvent
from dst_server.events.server import SavedEvent

from .base import FrozenModel, NonNegativeInt, ULIDValue
from .driver import DriverHealth
from .player import Player
from .telemetry import TelemetryProfile

type ClusterPhase = Literal[
    "waitingAgents",
    "preparing",
    "starting",
    "running",
    "degraded",
    "stopping",
    "stopped",
    "failed",
]


class ShardDesired(StrEnum):
    RUNNING = "running"
    STOPPED = "stopped"


class ShardPhase(StrEnum):
    UNAVAILABLE = "unavailable"
    STOPPED = "stopped"
    STARTING = "starting"
    RUNNING = "running"
    STOPPING = "stopping"
    FAILED = "failed"


class ShardRuntimeStatus(FrozenModel):
    name: Annotated[str, Field(min_length=1)]
    is_master: bool
    desired: ShardDesired
    phase: ShardPhase
    agent_incarnation: ULIDValue | None = None
    game_attempt: ULIDValue | None = None
    outdated_mods: tuple[str, ...] = ()
    pid: NonNegativeInt | None = None
    session_id: str | None = None
    ready: bool = False
    returncode: int | None = None
    driver_health: DriverHealth | None = None
    driver_error: str | None = None
    telemetry_profile: TelemetryProfile
    telemetry_invalid: NonNegativeInt = 0
    telemetry_dropped: NonNegativeInt = 0
    telemetry_duplicates: NonNegativeInt = 0
    telemetry_stale: NonNegativeInt = 0
    telemetry_gaps: NonNegativeInt = 0
    telemetry_last_event_ns: NonNegativeInt | None = None
    telemetry_last_presence_ns: NonNegativeInt | None = None
    last_active_at: AwareDatetime | None = None
    player_count: NonNegativeInt = 0
    client_count: NonNegativeInt = 0
    external_port: Annotated[int, Field(ge=1024, le=65535)] | None = None
    error_id: ULIDValue | None = None
    error: str | None = None


class ModUpdateStatus(FrozenModel):
    enabled: bool = True
    pending: bool = False
    updating: bool = False
    retry_in_seconds: Annotated[float, Field(ge=0, allow_inf_nan=False)] = 0.0
    error: str | None = None


class ClusterStatus(FrozenModel):
    epoch: ULIDValue
    phase: ClusterPhase
    prepared: bool = False
    busy: bool = False
    master: Annotated[str, Field(min_length=1)]
    missing_shards: tuple[str, ...] = ()
    shards: tuple[ShardRuntimeStatus, ...]
    mod_update: ModUpdateStatus = ModUpdateStatus()
    error_id: ULIDValue | None = None
    error: str | None = None


class LocatedPlayer(FrozenModel):
    shard: Annotated[str, Field(min_length=1)] | None
    player: Player


class ObservationCursor(FrozenModel):
    attempt: ULIDValue
    sequence: NonNegativeInt


class LogRecord(FrozenModel):
    shard: Annotated[str, Field(min_length=1)]
    game_attempt: ULIDValue
    sequence: NonNegativeInt
    observed_timestamp_ns: NonNegativeInt
    line: str


class LifecycleRecord(FrozenModel):
    shard: Annotated[str, Field(min_length=1)]
    game_attempt: ULIDValue
    sequence: NonNegativeInt
    observed_timestamp_ns: NonNegativeInt
    event: LifecycleEvent


class GameEventRecord(FrozenModel):
    shard: Annotated[str, Field(min_length=1)]
    game_attempt: ULIDValue
    sequence: NonNegativeInt
    observed_timestamp_ns: NonNegativeInt
    event: GameEvent


@dataclass(frozen=True, slots=True)
class ShardResult[T]:
    shard: str
    value: T | None = None
    error: ErrorInfo | None = None


@dataclass(frozen=True, slots=True)
class ClusterSaveResult:
    snapshot: int | None
    shards: tuple[tuple[str, SavedEvent], ...]
