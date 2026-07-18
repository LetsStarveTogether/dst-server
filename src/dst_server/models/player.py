from __future__ import annotations

from typing import Annotated

from pydantic import Field

from ..schema import (
    FiniteFloat,
    FrozenModel,
    Identifier,
    Name,
    NonNegativeFloat,
    NonNegativeInt,
    Percent,
    PositiveInt,
)
from .value import Entity, Position


class Stat(FrozenModel):
    current: FiniteFloat
    maximum: FiniteFloat
    percent: FiniteFloat


class Health(Stat):
    is_dead: bool
    is_invincible: bool


class Temperature(FrozenModel):
    current: FiniteFloat
    maximum: FiniteFloat


class Age(FrozenModel):
    seconds: NonNegativeFloat
    days: NonNegativeInt
    display_days: PositiveInt


class Vitals(FrozenModel):
    health: Health | None
    hunger: Stat | None
    sanity: Stat | None
    temperature: Temperature | None
    moisture: Stat | None


class PlayerState(FrozenModel):
    network_score: NonNegativeInt | None
    combat_target: Entity | None
    weapon: Entity | None
    mount: Entity | None
    follower_count: NonNegativeInt
    followers: Annotated[tuple[Entity, ...], Field(max_length=256)]
    skill_xp: NonNegativeInt | None
    available_skill_points: NonNegativeInt | None
    activated_skills: Annotated[
        tuple[Identifier, ...] | None,
        Field(max_length=256),
    ]


class Player(FrozenModel):
    userid: Identifier
    name: Name
    prefab: Identifier
    admin: bool
    moderator: bool
    is_ghost: bool | None
    position: Position | None
    age: Age | None
    vitals: Vitals | None
    state: PlayerState | None


class Item(FrozenModel):
    prefab: Identifier
    guid: PositiveInt
    skin: Identifier | None
    stack_size: PositiveInt
    moisture_percent: Percent | None
    uses_percent: FiniteFloat | None
    freshness_percent: FiniteFloat | None
    fuel_percent: FiniteFloat | None
    armor_percent: FiniteFloat | None
    charge_percent: Percent | None


class InventorySlot(FrozenModel):
    slot: PositiveInt
    item: Item


class EquipmentSlot(FrozenModel):
    slot: Annotated[str, Field(min_length=1)]
    item: Item


class Container(FrozenModel):
    prefab: Identifier
    guid: PositiveInt
    slots: tuple[InventorySlot, ...]


class Inventory(FrozenModel):
    userid: Identifier
    items: tuple[InventorySlot, ...]
    equipment: tuple[EquipmentSlot, ...]
    active_item: Item | None
    overflow: Container | None


__all__ = [
    "Age",
    "Container",
    "EquipmentSlot",
    "Health",
    "Inventory",
    "InventorySlot",
    "Item",
    "Player",
    "PlayerState",
    "Stat",
    "Temperature",
    "Vitals",
]
