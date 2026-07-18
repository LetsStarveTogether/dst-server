from __future__ import annotations

from dataclasses import dataclass
from typing import Annotated

from pydantic import Field, TypeAdapter

from .player import (
    PlayerActionEvent,
    PlayerAteEvent,
    PlayerCombatBlockedEvent,
    PlayerCombatHitEvent,
    PlayerCombatReceivedEvent,
    PlayerConditionChangedEvent,
    PlayerCraftedEvent,
    PlayerDeployedEvent,
    PlayerDisconnectedEvent,
    PlayerDroppedEvent,
    PlayerEquippedEvent,
    PlayerFinishedWorkEvent,
    PlayerFishedEvent,
    PlayerGhostedEvent,
    PlayerHarvestedEvent,
    PlayerHoundWarningEvent,
    PlayerIncidentEvent,
    PlayerMigrationStartedEvent,
    PlayerPickedEvent,
    PlayerPlantedEvent,
    PlayerRevivedEvent,
    PlayerShardEnteredEvent,
    PlayerShardLeftEvent,
    PlayerSkillChangedEvent,
    PlayerSpawnedEvent,
    PlayerUnequippedEvent,
)
from .world import (
    EntityDeathEvent,
    ShardBossDefeatedEvent,
    ShardConnectionChangedEvent,
    WorldRiftChangedEvent,
    WorldRiftUnlockedEvent,
    WorldStateChangedEvent,
)

type GameEvent = Annotated[
    PlayerShardEnteredEvent
    | PlayerShardLeftEvent
    | PlayerDisconnectedEvent
    | PlayerMigrationStartedEvent
    | PlayerSpawnedEvent
    | PlayerGhostedEvent
    | PlayerRevivedEvent
    | EntityDeathEvent
    | PlayerActionEvent
    | PlayerCombatHitEvent
    | PlayerCombatReceivedEvent
    | PlayerCombatBlockedEvent
    | WorldStateChangedEvent
    | ShardBossDefeatedEvent
    | ShardConnectionChangedEvent
    | PlayerCraftedEvent
    | PlayerAteEvent
    | PlayerPickedEvent
    | PlayerHarvestedEvent
    | PlayerFinishedWorkEvent
    | PlayerDeployedEvent
    | PlayerEquippedEvent
    | PlayerUnequippedEvent
    | PlayerDroppedEvent
    | PlayerConditionChangedEvent
    | PlayerIncidentEvent
    | PlayerFishedEvent
    | PlayerPlantedEvent
    | PlayerSkillChangedEvent
    | PlayerHoundWarningEvent
    | WorldRiftUnlockedEvent
    | WorldRiftChangedEvent,
    Field(discriminator="event"),
]

GAME_EVENT_ADAPTER = TypeAdapter(GameEvent)


@dataclass(frozen=True, slots=True)
class ObservedGameEvent:
    record: GameEvent
    observed_timestamp_ns: int


__all__ = ["GAME_EVENT_ADAPTER", "GameEvent", "ObservedGameEvent"]
