from typing import Annotated, Literal

from pydantic import Field

from dst_server.lua_codec import NonNegativeSafeLuaInteger, PositiveSafeLuaInteger
from dst_server.models import Position
from dst_server.models.base import FrozenModel, Identifier


class EntityRef(FrozenModel):
    prefab: Identifier
    guid: PositiveSafeLuaInteger
    userid: Identifier | None
    position: Position | None


class ItemRef(FrozenModel):
    prefab: Identifier
    guid: PositiveSafeLuaInteger
    skin: Identifier | None
    stack_size: PositiveSafeLuaInteger


class PlayerData(FrozenModel):
    player: EntityRef


class CausedData(PlayerData):
    caused_by_action_sequence: PositiveSafeLuaInteger | None


class EventRecord[DataT](FrozenModel):
    v: Literal[2]
    nonce: Annotated[
        str,
        Field(pattern=r"^[0-7][0-9A-HJKMNP-TV-Z]{25}$"),
    ]
    generation: NonNegativeSafeLuaInteger
    session_id: Identifier | None
    seq: PositiveSafeLuaInteger
    event: str
    tick: NonNegativeSafeLuaInteger
    monotonic_ms: NonNegativeSafeLuaInteger
    cycle: NonNegativeSafeLuaInteger | None
    data: DataT
