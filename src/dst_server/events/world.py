from __future__ import annotations

from typing import Annotated, Literal

from pydantic import Field

from ..schema import FrozenModel, Identifier, Name, NonNegativeInt, PositiveInt
from .base import EntityRef, EventRecord


class EntityDeathData(FrozenModel):
    victim: EntityRef
    cause: Annotated[str, Field(max_length=256)] | None
    afflicter: EntityRef | None
    attributed_player: EntityRef | None
    corpsing: bool
    caused_by_action_sequence: PositiveInt | None


class CycleWorldState(FrozenModel):
    name: Literal["cycles"]
    value: NonNegativeInt


class PhaseWorldState(FrozenModel):
    name: Literal["phase", "cavephase"]
    value: Literal["day", "dusk", "night"]


class SeasonWorldState(FrozenModel):
    name: Literal["season"]
    value: Literal["autumn", "winter", "spring", "summer"]


class MoonWorldState(FrozenModel):
    name: Literal["moonphase", "cavemoonphase"]
    value: Literal["new", "quarter", "half", "threequarter", "full"]


class NightmareWorldState(FrozenModel):
    name: Literal["nightmarephase"]
    value: Literal["none", "calm", "warn", "wild", "dawn"]


class BooleanWorldState(FrozenModel):
    name: Literal[
        "israining",
        "issnowing",
        "isacidraining",
        "islunarhailing",
    ]
    value: bool


type WorldStateData = Annotated[
    CycleWorldState
    | PhaseWorldState
    | SeasonWorldState
    | MoonWorldState
    | NightmareWorldState
    | BooleanWorldState,
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


class WorldRiftUnlockedData(FrozenModel):
    kind: Literal["lunar", "shadow"]


class WorldRiftChangedData(FrozenModel):
    rift: EntityRef
    active: bool


class EntityDeathEvent(EventRecord[EntityDeathData]):
    event: Literal["dst.entity.death"]


class WorldStateChangedEvent(EventRecord[WorldStateData]):
    event: Literal["dst.world.state_changed"]


class ShardBossDefeatedEvent(EventRecord[ShardBossDefeatedData]):
    event: Literal["dst.world.shard_boss_defeated"]


class ShardConnectionChangedEvent(EventRecord[ShardConnectionData]):
    event: Literal["dst.shard.connection_changed"]


class WorldRiftUnlockedEvent(EventRecord[WorldRiftUnlockedData]):
    event: Literal["dst.world.rift_unlocked"]


class WorldRiftChangedEvent(EventRecord[WorldRiftChangedData]):
    event: Literal["dst.world.rift_changed"]


__all__ = [
    "EntityDeathEvent",
    "ShardBossDefeatedEvent",
    "ShardConnectionChangedEvent",
    "WorldRiftChangedEvent",
    "WorldRiftUnlockedEvent",
    "WorldStateChangedEvent",
    "WorldStateData",
]
