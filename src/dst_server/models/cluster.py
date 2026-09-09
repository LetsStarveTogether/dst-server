from dataclasses import dataclass
from enum import StrEnum
from typing import Annotated, Literal

from pydantic import Field

from dst_server.configuration.models import ClusterConfig
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
    RETRY_WAIT = "retryWait"
    FAILED = "failed"


class ShardRuntimeStatus(FrozenModel):
    name: Annotated[str, Field(min_length=1)]
    is_master: bool
    desired: ShardDesired
    phase: ShardPhase
    agent_incarnation: ULIDValue | None = None
    game_attempt: ULIDValue | None = None
    pid: NonNegativeInt | None = None
    session_id: str | None = None
    ready: bool = False
    returncode: int | None = None
    retry_attempt: Annotated[int, Field(ge=0, le=5)] = 0
    stable_since_ns: NonNegativeInt | None = None
    driver_health: DriverHealth | None = None
    driver_error: str | None = None
    telemetry_profile: TelemetryProfile
    telemetry_invalid: NonNegativeInt = 0
    telemetry_dropped: NonNegativeInt = 0
    external_port: Annotated[int, Field(ge=1024, le=65535)] | None = None
    error_id: ULIDValue | None = None
    error: str | None = None


class ClusterStatus(FrozenModel):
    epoch: ULIDValue
    phase: ClusterPhase
    revision: ULIDValue | None = None
    prepared_revision: ULIDValue | None = None
    master: Annotated[str, Field(min_length=1)]
    missing_shards: tuple[str, ...] = ()
    shards: tuple[ShardRuntimeStatus, ...]
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
class ConfigurationSnapshot:
    revision: ULIDValue
    configuration: ClusterConfig


@dataclass(frozen=True, slots=True)
class InvalidConfiguration:
    revision: ULIDValue
    fields: tuple[tuple[str, ...], ...]


type ConfigurationRead = ConfigurationSnapshot | InvalidConfiguration


@dataclass(frozen=True, slots=True)
class ShardResult[T]:
    shard: str
    value: T | None = None
    error: ErrorInfo | None = None


@dataclass(frozen=True, slots=True)
class ClusterSaveResult:
    snapshot: int | None
    shards: tuple[tuple[str, SavedEvent], ...]
