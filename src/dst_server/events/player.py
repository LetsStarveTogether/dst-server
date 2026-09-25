from typing import Annotated, Literal

from pydantic import Field

from dst_server.lua_codec import NonNegativeSafeLuaInteger
from dst_server.models import Position
from dst_server.models.base import (
    FiniteFloat,
    FrozenModel,
    Identifier,
)

from .base import EntityRef, EventRecord, ItemRef, PlayerData

type EventText = Annotated[str, Field(max_length=256)]


class ChatData(FrozenModel):
    userid: Identifier | None
    name: str | None
    prefab: Identifier | None
    message: str
    whisper: bool
    emote: bool
    player: EntityRef | None


class PlayerRequestData(FrozenModel):
    userid: Identifier
    player: EntityRef | None


class EmoteRequestedData(PlayerRequestData):
    emote: Identifier


class AppearanceData(FrozenModel):
    prefab: Identifier
    skin_base: Identifier | None
    clothing_body: Identifier | None
    clothing_hand: Identifier | None
    clothing_legs: Identifier | None
    clothing_feet: Identifier | None


class AppearanceRequestedData(PlayerRequestData):
    requested: AppearanceData
    validated: AppearanceData


class DisconnectedData(PlayerData):
    expected: bool


class MigrationStartedData(PlayerData):
    destination_shard_id: Identifier
    portal_id: NonNegativeSafeLuaInteger | Identifier | None
    destination: Position | None


class GhostedData(PlayerData):
    corpse: bool


class RevivedData(GhostedData):
    method: Literal["ghost", "corpse", "charlie"]
    reviver: EntityRef | None


class ActionData(FrozenModel):
    action_id: Identifier
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
    from_doattack: bool | None


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


class CraftedData(PlayerData):
    item: ItemRef
    recipe: Identifier
    kind: Literal["item", "structure"]
    skin: Identifier | None


class AteData(PlayerData):
    food: ItemRef
    feeder: EntityRef | None


class PickedData(PlayerData):
    source: EntityRef
    loot: tuple[ItemRef, ...]


class HarvestedData(PlayerData):
    source: EntityRef


class FinishedWorkData(PlayerData):
    target: EntityRef
    action_id: Identifier | None


class DeployedData(PlayerData):
    prefab: Identifier


class EquippedData(PlayerData):
    item: ItemRef
    slot: Identifier


class UnequippedData(PlayerData):
    item: ItemRef | None
    slot: Identifier
    slip: bool


class DroppedData(PlayerData):
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


class FishedData(PlayerData):
    fish: ItemRef
    method: Literal["inland", "ocean"]


class PlantedData(PlayerData):
    position: Position


class SkillChangedData(PlayerData):
    skill: Identifier
    active: bool


class HoundWarningData(PlayerData):
    warning_type: Annotated[int, Field(ge=0, le=8)]


class ShardEnteredEvent(EventRecord[PlayerData]):
    event: Literal["dst.player.shard_entered"]


class ChatEvent(EventRecord[ChatData]):
    event: Literal["dst.player.chat"]


class EmoteRequestedEvent(EventRecord[EmoteRequestedData]):
    event: Literal["dst.player.emote_requested"]


class RescueRequestedEvent(EventRecord[PlayerRequestData]):
    event: Literal["dst.player.rescue_requested"]


class AppearanceRequestedEvent(EventRecord[AppearanceRequestedData]):
    event: Literal["dst.player.appearance_requested"]


class PlayerLoadedEvent(EventRecord[PlayerData]):
    event: Literal["dst.player.loaded"]
    session_id: Identifier


class ShardLeftEvent(EventRecord[PlayerData]):
    event: Literal["dst.player.shard_left"]


class DisconnectedEvent(EventRecord[DisconnectedData]):
    event: Literal["dst.player.disconnected"]


class MigrationStartedEvent(EventRecord[MigrationStartedData]):
    event: Literal["dst.player.migration_started"]


class SpawnedEvent(EventRecord[PlayerData]):
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
