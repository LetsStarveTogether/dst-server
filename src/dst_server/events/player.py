from __future__ import annotations

from typing import Annotated, Literal

from pydantic import Field

from dst_server.models import Position
from dst_server.models.base import (
    FiniteFloat,
    FrozenModel,
    Identifier,
    NonNegativeInt,
    PositiveInt,
)

from .base import CausedData, EntityRef, EventRecord, ItemRef, PlayerData

type EventText = Annotated[str, Field(max_length=256)]


class DisconnectedData(PlayerData):
    expected: bool


class MigrationStartedData(PlayerData):
    destination_shard_id: Identifier
    portal_id: NonNegativeInt | None
    destination: Position | None


class SpawnedData(PlayerData):
    mode: Identifier


class GhostedData(PlayerData):
    corpse: bool


class RevivedData(GhostedData):
    reviver: EntityRef | None


class ActionData(FrozenModel):
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


class CombatHitData(CombatData):
    target: EntityRef
    damage_resolved: FiniteFloat | None
    redirected: EntityRef | None


class CombatReceivedData(CombatData):
    attacker: EntityRef | None
    damage_resolved: FiniteFloat | None
    original_damage: FiniteFloat | None
    redirected: EntityRef | None


class CombatBlockedData(CombatData):
    attacker: EntityRef | None
    original_damage: FiniteFloat | None


class CraftedData(CausedData):
    item: ItemRef
    recipe: Identifier
    kind: Literal["item", "structure"]
    skin: Identifier | None


class AteData(CausedData):
    food: ItemRef
    feeder: EntityRef | None


class PickedData(CausedData):
    source: EntityRef
    loot: tuple[ItemRef, ...]


class HarvestedData(CausedData):
    source: EntityRef


class FinishedWorkData(CausedData):
    target: EntityRef
    action_id: Identifier


class DeployedData(CausedData):
    prefab: Identifier


class EquippedData(CausedData):
    item: ItemRef
    slot: Identifier


class UnequippedData(EquippedData):
    slip: bool


class DroppedData(CausedData):
    item: ItemRef


class BooleanConditionData(PlayerData):
    condition: Literal[
        "starving",
        "freezing",
        "overheating",
        "fire_damage",
        "lunar_burn",
    ]
    active: bool


class SanityConditionData(PlayerData):
    condition: Literal["sanity"]
    state: Literal["sane", "insane", "enlightened"]


type ConditionData = Annotated[
    BooleanConditionData | SanityConditionData,
    Field(discriminator="condition"),
]


class IncidentData(PlayerData):
    kind: Literal["sink", "fall_in_void"]
    source: EntityRef | None
    destination: Position | None


class FishedData(CausedData):
    fish: ItemRef
    method: Literal["inland", "ocean"]


class PlantedData(CausedData):
    position: Position


class SkillChangedData(PlayerData):
    skill: Identifier
    active: bool


class HoundWarningData(PlayerData):
    warning_type: Annotated[int, Field(ge=0, le=8)]


class ShardEnteredEvent(EventRecord[PlayerData]):
    event: Literal["dst.player.shard_entered"]


class ShardLeftEvent(EventRecord[PlayerData]):
    event: Literal["dst.player.shard_left"]


class DisconnectedEvent(EventRecord[DisconnectedData]):
    event: Literal["dst.player.disconnected"]


class MigrationStartedEvent(EventRecord[MigrationStartedData]):
    event: Literal["dst.player.migration_started"]


class SpawnedEvent(EventRecord[SpawnedData]):
    event: Literal["dst.player.spawned"]


class GhostedEvent(EventRecord[GhostedData]):
    event: Literal["dst.player.ghosted"]


class RevivedEvent(EventRecord[RevivedData]):
    event: Literal["dst.player.revived"]


class ActionEvent(EventRecord[ActionData]):
    event: Literal["dst.player.action"]


class CombatHitEvent(EventRecord[CombatHitData]):
    event: Literal["dst.player.combat_hit"]


class CombatReceivedEvent(EventRecord[CombatReceivedData]):
    event: Literal["dst.player.combat_received"]


class CombatBlockedEvent(EventRecord[CombatBlockedData]):
    event: Literal["dst.player.combat_blocked"]


class CraftedEvent(EventRecord[CraftedData]):
    event: Literal["dst.player.crafted"]


class AteEvent(EventRecord[AteData]):
    event: Literal["dst.player.ate"]


class PickedEvent(EventRecord[PickedData]):
    event: Literal["dst.player.picked"]


class HarvestedEvent(EventRecord[HarvestedData]):
    event: Literal["dst.player.harvested"]


class FinishedWorkEvent(EventRecord[FinishedWorkData]):
    event: Literal["dst.player.finished_work"]


class DeployedEvent(EventRecord[DeployedData]):
    event: Literal["dst.player.deployed"]


class EquippedEvent(EventRecord[EquippedData]):
    event: Literal["dst.player.equipped"]


class UnequippedEvent(EventRecord[UnequippedData]):
    event: Literal["dst.player.unequipped"]


class DroppedEvent(EventRecord[DroppedData]):
    event: Literal["dst.player.dropped"]


class ConditionChangedEvent(EventRecord[ConditionData]):
    event: Literal["dst.player.condition_changed"]


class IncidentEvent(EventRecord[IncidentData]):
    event: Literal["dst.player.incident"]


class FishedEvent(EventRecord[FishedData]):
    event: Literal["dst.player.fished"]


class PlantedEvent(EventRecord[PlantedData]):
    event: Literal["dst.player.planted"]


class SkillChangedEvent(EventRecord[SkillChangedData]):
    event: Literal["dst.player.skill_changed"]


class HoundWarningEvent(EventRecord[HoundWarningData]):
    event: Literal["dst.player.hound_warning"]


__all__ = [
    "ActionData",
    "ActionEvent",
    "AteData",
    "AteEvent",
    "BooleanConditionData",
    "CombatBlockedData",
    "CombatBlockedEvent",
    "CombatData",
    "CombatHitData",
    "CombatHitEvent",
    "CombatReceivedData",
    "CombatReceivedEvent",
    "ConditionChangedEvent",
    "ConditionData",
    "CraftedData",
    "CraftedEvent",
    "DeployedData",
    "DeployedEvent",
    "DisconnectedData",
    "DisconnectedEvent",
    "DroppedData",
    "DroppedEvent",
    "EquippedData",
    "EquippedEvent",
    "FinishedWorkData",
    "FinishedWorkEvent",
    "FishedData",
    "FishedEvent",
    "GhostedData",
    "GhostedEvent",
    "HarvestedData",
    "HarvestedEvent",
    "HoundWarningData",
    "HoundWarningEvent",
    "IncidentData",
    "IncidentEvent",
    "MigrationStartedData",
    "MigrationStartedEvent",
    "PickedData",
    "PickedEvent",
    "PlantedData",
    "PlantedEvent",
    "RevivedData",
    "RevivedEvent",
    "SanityConditionData",
    "ShardEnteredEvent",
    "ShardLeftEvent",
    "SkillChangedData",
    "SkillChangedEvent",
    "SpawnedData",
    "SpawnedEvent",
    "SpecialDamage",
    "UnequippedData",
    "UnequippedEvent",
]
