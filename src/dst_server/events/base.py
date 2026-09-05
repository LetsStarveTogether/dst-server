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


class DriverDiagnostic(FrozenModel):
    stage: Annotated[str, Field(pattern=r"^[a-z][a-z0-9_.:]{0,127}$")]
    message: Literal[
        "callback_failed",
        "encoding_failed",
        "event_too_large",
        "installation_failed",
    ]
    count: PositiveInt


class EventRecord[DataT](FrozenModel):
    v: Literal[2]
    nonce: Annotated[
        str,
        Field(pattern=r"^[0-7][0-9A-HJKMNP-TV-Z]{25}$"),
    ]
    generation: NonNegativeInt
    session_id: Identifier | None
    seq: PositiveInt
    event: str
    tick: NonNegativeInt
    monotonic_ms: NonNegativeInt
    cycle: NonNegativeInt | None
    data: DataT
