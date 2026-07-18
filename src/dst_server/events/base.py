from __future__ import annotations

from typing import Annotated, Literal

from pydantic import Field

from dst_server.models import Position
from dst_server.schema import FrozenModel, Identifier, NonNegativeInt, PositiveInt

type TelemetryProfile = Literal["off", "critical", "history"]

DEFAULT_ACTIONS = (
    "ACTIVATE",
    "ADDFUEL",
    "ATTACK",
    "BUILD",
    "CASTAOE",
    "CASTSPELL",
    "CHOP",
    "CONSTRUCT",
    "COOK",
    "DEPLOY",
    "DIG",
    "EXTINGUISH",
    "FERTILIZE",
    "FISH",
    "FISH_OCEAN",
    "GIVE",
    "GIVETOPLAYER",
    "HAMMER",
    "HARVEST",
    "HEAL",
    "LIGHT",
    "MIGRATE",
    "MINE",
    "MURDER",
    "PICK",
    "PICKUP",
    "PLANT",
    "REPAIR",
    "REVIVE_CORPSE",
    "TELEPORT",
    "UNLOCK",
    "UPGRADE",
)


class TelemetrySettings(FrozenModel):
    profile: TelemetryProfile = "history"
    actions: tuple[Identifier, ...] = DEFAULT_ACTIONS


class DriverHealth(FrozenModel):
    protocol: Literal[1]
    installed: bool
    profile: TelemetryProfile
    events_emitted: NonNegativeInt
    errors: NonNegativeInt
    players: NonNegativeInt
    action_hook: bool
    shard_hook: bool


class EntityRef(FrozenModel):
    prefab: Identifier
    guid: PositiveInt
    userid: Identifier | None
    position: Position | None


class ItemRef(FrozenModel):
    prefab: Identifier
    guid: PositiveInt
    skin: Identifier | None
    stack_size: PositiveInt


class PlayerData(FrozenModel):
    player: EntityRef


class CausedData(PlayerData):
    caused_by_action_sequence: PositiveInt | None


class EventRecord[DataT](FrozenModel):
    v: Literal[1]
    nonce: Annotated[str, Field(min_length=16, max_length=128)]
    seq: PositiveInt
    event: str
    tick: NonNegativeInt
    monotonic_ms: NonNegativeInt
    cycle: NonNegativeInt | None
    data: DataT


__all__ = [
    "DEFAULT_ACTIONS",
    "CausedData",
    "DriverHealth",
    "EntityRef",
    "EventRecord",
    "ItemRef",
    "PlayerData",
    "TelemetryProfile",
    "TelemetrySettings",
]
