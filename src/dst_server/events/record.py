from __future__ import annotations

from dataclasses import dataclass
from typing import Annotated

from pydantic import Field, TypeAdapter

from .player import (
    ActionEvent,
    AteEvent,
    CombatBlockedEvent,
    CombatHitEvent,
    CombatReceivedEvent,
    ConditionChangedEvent,
    CraftedEvent,
    DeployedEvent,
    DisconnectedEvent,
    DroppedEvent,
    EquippedEvent,
    FinishedWorkEvent,
    FishedEvent,
    GhostedEvent,
    HarvestedEvent,
    HoundWarningEvent,
    IncidentEvent,
    MigrationStartedEvent,
    PickedEvent,
    PlantedEvent,
    RevivedEvent,
    ShardEnteredEvent,
    ShardLeftEvent,
    SkillChangedEvent,
    SpawnedEvent,
    UnequippedEvent,
)
from .world import (
    EntityDeathEvent,
    RiftChangedEvent,
    RiftUnlockedEvent,
    ShardBossDefeatedEvent,
    ShardConnectionChangedEvent,
    StateChangedEvent,
)

type GameEvent = Annotated[
    ShardEnteredEvent
    | ShardLeftEvent
    | DisconnectedEvent
    | MigrationStartedEvent
    | SpawnedEvent
    | GhostedEvent
    | RevivedEvent
    | EntityDeathEvent
    | ActionEvent
    | CombatHitEvent
    | CombatReceivedEvent
    | CombatBlockedEvent
    | StateChangedEvent
    | ShardBossDefeatedEvent
    | ShardConnectionChangedEvent
    | CraftedEvent
    | AteEvent
    | PickedEvent
    | HarvestedEvent
    | FinishedWorkEvent
    | DeployedEvent
    | EquippedEvent
    | UnequippedEvent
    | DroppedEvent
    | ConditionChangedEvent
    | IncidentEvent
    | FishedEvent
    | PlantedEvent
    | SkillChangedEvent
    | HoundWarningEvent
    | RiftUnlockedEvent
    | RiftChangedEvent,
    Field(discriminator="event"),
]

GAME_EVENT_ADAPTER = TypeAdapter(GameEvent)


@dataclass(frozen=True, slots=True)
class ObservedGameEvent:
    record: GameEvent
    observed_timestamp_ns: int


__all__ = ["GAME_EVENT_ADAPTER", "GameEvent", "ObservedGameEvent"]
