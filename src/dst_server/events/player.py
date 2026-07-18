from __future__ import annotations

from typing import Annotated, Literal

from pydantic import Field

from ..models import Position
from ..schema import FiniteFloat, FrozenModel, Identifier, NonNegativeInt, PositiveInt
from .base import CausedData, EntityRef, EventRecord, ItemRef, PlayerData

type EventText = Annotated[str, Field(max_length=256)]


class PlayerDisconnectedData(PlayerData):
    expected: bool


class PlayerMigrationStartedData(PlayerData):
    destination_shard_id: Identifier
    portal_id: NonNegativeInt | None
    destination: Position | None


class PlayerSpawnedData(PlayerData):
    mode: Identifier


class PlayerGhostedData(PlayerData):
    corpse: bool


class PlayerRevivedData(PlayerGhostedData):
    reviver: EntityRef | None


class PlayerActionData(FrozenModel):
    action_id: Identifier
    action_sequence: PositiveInt
    success: bool
    reason: EventText | None
    actor: EntityRef
    target: EntityRef | None
    initial_target_owner: EntityRef | None
    inventory_object: ItemRef | None
    position: Position | None
    recipe: Identifier | None
    forced: bool


class SpecialDamage(FrozenModel):
    kind: Identifier
    value: FiniteFloat


class CombatData(PlayerData):
    damage: FiniteFloat | None
    weapon: EntityRef | None
    stimuli: Identifier | None
    special_damage: tuple[SpecialDamage, ...]
    caused_by_action_sequence: PositiveInt | None


class PlayerCombatHitData(CombatData):
    target: EntityRef
    damage_resolved: FiniteFloat
    redirected: EntityRef | None


class PlayerCombatReceivedData(CombatData):
    attacker: EntityRef | None
    damage_resolved: FiniteFloat
    original_damage: FiniteFloat | None
    redirected: EntityRef | None


class PlayerCombatBlockedData(CombatData):
    attacker: EntityRef | None
    original_damage: FiniteFloat | None


class PlayerCraftedData(CausedData):
    item: ItemRef
    recipe: Identifier
    kind: Literal["item", "structure"]
    skin: Identifier | None


class PlayerAteData(CausedData):
    food: ItemRef
    feeder: EntityRef | None


class PlayerPickedData(CausedData):
    source: EntityRef
    loot: tuple[ItemRef, ...]


class PlayerHarvestedData(CausedData):
    source: EntityRef


class PlayerFinishedWorkData(CausedData):
    target: EntityRef
    action_id: Identifier


class PlayerDeployedData(CausedData):
    prefab: Identifier


class PlayerEquippedData(CausedData):
    item: ItemRef
    slot: Identifier


class PlayerUnequippedData(PlayerEquippedData):
    slip: bool


class PlayerDroppedData(CausedData):
    item: ItemRef


class BooleanPlayerConditionData(PlayerData):
    condition: Literal[
        "starving",
        "freezing",
        "overheating",
        "fire_damage",
        "lunar_burn",
    ]
    active: bool


class SanityPlayerConditionData(PlayerData):
    condition: Literal["sanity"]
    state: Literal["sane", "insane", "enlightened"]


type PlayerConditionData = Annotated[
    BooleanPlayerConditionData | SanityPlayerConditionData,
    Field(discriminator="condition"),
]


class PlayerIncidentData(PlayerData):
    kind: Literal["sink", "fall_in_void"]
    source: EntityRef | None
    destination: Position | None


class PlayerFishedData(CausedData):
    fish: ItemRef
    method: Literal["inland", "ocean"]


class PlayerPlantedData(CausedData):
    position: Position


class PlayerSkillChangedData(PlayerData):
    skill: Identifier
    active: bool


class PlayerHoundWarningData(PlayerData):
    warning_type: Annotated[int, Field(ge=0, le=8)]


class PlayerShardEnteredEvent(EventRecord[PlayerData]):
    event: Literal["dst.player.shard_entered"]


class PlayerShardLeftEvent(EventRecord[PlayerData]):
    event: Literal["dst.player.shard_left"]


class PlayerDisconnectedEvent(EventRecord[PlayerDisconnectedData]):
    event: Literal["dst.player.disconnected"]


class PlayerMigrationStartedEvent(EventRecord[PlayerMigrationStartedData]):
    event: Literal["dst.player.migration_started"]


class PlayerSpawnedEvent(EventRecord[PlayerSpawnedData]):
    event: Literal["dst.player.spawned"]


class PlayerGhostedEvent(EventRecord[PlayerGhostedData]):
    event: Literal["dst.player.ghosted"]


class PlayerRevivedEvent(EventRecord[PlayerRevivedData]):
    event: Literal["dst.player.revived"]


class PlayerActionEvent(EventRecord[PlayerActionData]):
    event: Literal["dst.player.action"]


class PlayerCombatHitEvent(EventRecord[PlayerCombatHitData]):
    event: Literal["dst.player.combat_hit"]


class PlayerCombatReceivedEvent(EventRecord[PlayerCombatReceivedData]):
    event: Literal["dst.player.combat_received"]


class PlayerCombatBlockedEvent(EventRecord[PlayerCombatBlockedData]):
    event: Literal["dst.player.combat_blocked"]


class PlayerCraftedEvent(EventRecord[PlayerCraftedData]):
    event: Literal["dst.player.crafted"]


class PlayerAteEvent(EventRecord[PlayerAteData]):
    event: Literal["dst.player.ate"]


class PlayerPickedEvent(EventRecord[PlayerPickedData]):
    event: Literal["dst.player.picked"]


class PlayerHarvestedEvent(EventRecord[PlayerHarvestedData]):
    event: Literal["dst.player.harvested"]


class PlayerFinishedWorkEvent(EventRecord[PlayerFinishedWorkData]):
    event: Literal["dst.player.finished_work"]


class PlayerDeployedEvent(EventRecord[PlayerDeployedData]):
    event: Literal["dst.player.deployed"]


class PlayerEquippedEvent(EventRecord[PlayerEquippedData]):
    event: Literal["dst.player.equipped"]


class PlayerUnequippedEvent(EventRecord[PlayerUnequippedData]):
    event: Literal["dst.player.unequipped"]


class PlayerDroppedEvent(EventRecord[PlayerDroppedData]):
    event: Literal["dst.player.dropped"]


class PlayerConditionChangedEvent(EventRecord[PlayerConditionData]):
    event: Literal["dst.player.condition_changed"]


class PlayerIncidentEvent(EventRecord[PlayerIncidentData]):
    event: Literal["dst.player.incident"]


class PlayerFishedEvent(EventRecord[PlayerFishedData]):
    event: Literal["dst.player.fished"]


class PlayerPlantedEvent(EventRecord[PlayerPlantedData]):
    event: Literal["dst.player.planted"]


class PlayerSkillChangedEvent(EventRecord[PlayerSkillChangedData]):
    event: Literal["dst.player.skill_changed"]


class PlayerHoundWarningEvent(EventRecord[PlayerHoundWarningData]):
    event: Literal["dst.player.hound_warning"]


__all__ = [name for name in globals() if name.startswith("Player")]
