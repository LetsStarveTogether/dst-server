from typing import Annotated, Literal

from pydantic import Field
from ulid import ULID

from dst_server.events import GameEvent
from dst_server.events.server import Event as LifecycleEvent
from dst_server.game.rpc import DriverHealth
from dst_server.models import Player
from dst_server.models.base import FrozenModel, NonNegativeInt
from dst_server.telemetry.config import TelemetryProfile
from dst_server.telemetry.delivery import DeliveryStatus

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
type ShardDesired = Literal["running", "stopped"]
type ShardPhase = Literal[
    "unavailable",
    "stopped",
    "starting",
    "running",
    "stopping",
    "retryWait",
    "failed",
]


class ShardRuntimeStatus(FrozenModel):
    name: Annotated[str, Field(min_length=1)]
    is_master: bool
    desired: ShardDesired
    phase: ShardPhase
    agent_incarnation: ULID | None = None
    game_attempt: ULID | None = None
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
    telemetry_delivery: DeliveryStatus | None = None
    external_port: Annotated[int, Field(ge=1024, le=65535)] | None = None
    error_id: ULID | None = None
    error: str | None = None


class ClusterStatus(FrozenModel):
    epoch: ULID
    phase: ClusterPhase
    revision: ULID | None = None
    prepared_revision: ULID | None = None
    master: Annotated[str, Field(min_length=1)]
    missing_shards: tuple[str, ...] = ()
    shards: tuple[ShardRuntimeStatus, ...]
    error_id: ULID | None = None
    error: str | None = None


class LocatedPlayer(FrozenModel):
    shard: Annotated[str, Field(min_length=1)] | None
    player: Player


class LogRecord(FrozenModel):
    shard: Annotated[str, Field(min_length=1)]
    game_attempt: ULID
    sequence: NonNegativeInt
    observed_timestamp_ns: NonNegativeInt
    line: str


class LifecycleRecord(FrozenModel):
    shard: Annotated[str, Field(min_length=1)]
    game_attempt: ULID
    sequence: NonNegativeInt
    observed_timestamp_ns: NonNegativeInt
    event: LifecycleEvent


class GameEventRecord(FrozenModel):
    shard: Annotated[str, Field(min_length=1)]
    game_attempt: ULID
    sequence: NonNegativeInt
    observed_timestamp_ns: NonNegativeInt
    event: GameEvent
