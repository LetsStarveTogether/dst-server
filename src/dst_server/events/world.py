from typing import Annotated, Literal

from pydantic import Field

from dst_server.models.base import (
    FrozenModel,
    Identifier,
    Name,
    NonNegativeInt,
    PositiveInt,
)

from .base import EntityRef, EventRecord


class EntityDeathData(FrozenModel):
    victim: EntityRef
    cause: Annotated[str, Field(max_length=256)] | None
    afflicter: EntityRef | None
    attributed_player: EntityRef | None
    corpsing: bool
    caused_by_action_sequence: PositiveInt | None


class CycleState(FrozenModel):
    name: Literal["cycles"]
    value: NonNegativeInt


class PhaseState(FrozenModel):
    name: Literal["phase", "cavephase"]
    value: Literal["day", "dusk", "night"]


class SeasonState(FrozenModel):
    name: Literal["season"]
    value: Literal["autumn", "winter", "spring", "summer"]


class MoonState(FrozenModel):
    name: Literal["moonphase", "cavemoonphase"]
    value: Literal["new", "quarter", "half", "threequarter", "full"]


class NightmareState(FrozenModel):
    name: Literal["nightmarephase"]
    value: Literal["none", "calm", "warn", "wild", "dawn"]


class BooleanState(FrozenModel):
    name: Literal[
        "israining",
        "issnowing",
        "isacidraining",
        "islunarhailing",
    ]
    value: bool


type StateData = Annotated[
    CycleState | PhaseState | SeasonState | MoonState | NightmareState | BooleanState,
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


class EntityDeathEvent(EventRecord[EntityDeathData]):
    event: Literal["dst.entity.death"]


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
