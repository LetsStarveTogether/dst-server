from typing import Annotated, Literal

from pydantic import Field

from dst_server.lua_codec import NonNegativeSafeLuaInteger, PositiveSafeLuaInteger
from dst_server.models.base import (
    FrozenModel,
    Identifier,
    Name,
)
from dst_server.models.driver import DriverDiagnostic

from .base import EntityRef, EventRecord


class EntityDeathData(FrozenModel):
    victim: EntityRef
    cause: Annotated[str, Field(max_length=256)] | None
    afflicter: EntityRef | None
    attributed_player: EntityRef | None
    corpsing: bool
    caused_by_action_sequence: PositiveSafeLuaInteger | None


class CycleState(FrozenModel):
    name: Literal["cycles"]
    value: NonNegativeSafeLuaInteger


class IdentifierState(FrozenModel):
    name: Literal[
        "phase", "cavephase", "season", "moonphase", "cavemoonphase", "nightmarephase"
    ]
    value: Identifier


class BooleanState(FrozenModel):
    name: Literal[
        "israining",
        "issnowing",
        "isacidraining",
        "islunarhailing",
    ]
    value: bool


type StateData = Annotated[
    CycleState | IdentifierState | BooleanState,
    Field(discriminator="name"),
]


class ShardBossDefeatedData(FrozenModel):
    prefab: Identifier
    shard_id: Identifier


class ShardConnectionData(FrozenModel):
    shard_id: Identifier
    name: Name
    ready: bool
    tags: tuple[Identifier, ...]


class RiftUnlockedData(FrozenModel):
    kind: Literal["lunar", "shadow"]


class RiftChangedData(FrozenModel):
    rift: EntityRef
    active: bool


class ModOutdatedData(FrozenModel):
    name: Annotated[str, Field(min_length=1, max_length=4096)]


class ServerPauseData(FrozenModel):
    domain: Literal["server"]
    pause: bool
    autopause: bool
    gameautopause: bool
    source: str | None


class SimulationPauseData(FrozenModel):
    domain: Literal["simulation"]
    paused: bool


type PauseData = Annotated[
    ServerPauseData | SimulationPauseData, Field(discriminator="domain")
]


class PauseChangedEvent(EventRecord[PauseData]):
    event: Literal["dst.server.pause_changed"]


class ModOutdatedEvent(EventRecord[ModOutdatedData]):
    event: Literal["dst.mod.outdated"]


class EntityDeathEvent(EventRecord[EntityDeathData]):
    event: Literal["dst.entity.death"]


class TelemetryErrorEvent(EventRecord[DriverDiagnostic]):
    event: Literal["dst.telemetry.error"]


class StateChangedEvent(EventRecord[StateData]):
    event: Literal["dst.world.state_changed"]


class ShardBossDefeatedEvent(EventRecord[ShardBossDefeatedData]):
    event: Literal["dst.world.shard_boss_defeated"]


class ShardConnectionChangedEvent(EventRecord[ShardConnectionData]):
    event: Literal["dst.shard.connection_changed"]


class RiftUnlockedEvent(EventRecord[RiftUnlockedData]):
    event: Literal["dst.world.rift_unlocked"]


class RiftChangedEvent(EventRecord[RiftChangedData]):
    event: Literal["dst.world.rift_changed"]
