from __future__ import annotations

from typing import Annotated, Literal

from pydantic import Field

from dst_server.models import Position
from dst_server.models.base import FrozenModel, Identifier, NonNegativeInt, PositiveInt


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
    nonce: Annotated[
        str,
        Field(pattern=r"^[0-7][0-9A-HJKMNP-TV-Z]{25}$"),
    ]
    seq: PositiveInt
    event: str
    tick: NonNegativeInt
    monotonic_ms: NonNegativeInt
    cycle: NonNegativeInt | None
    data: DataT


__all__ = [
    "CausedData",
    "EntityRef",
    "EventRecord",
    "ItemRef",
    "PlayerData",
]
