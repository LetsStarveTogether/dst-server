from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, JsonValue, TypeAdapter

from dst_server.events import DriverHealth
from dst_server.models import Inventory, Mod, Player, Room, Runtime, ShardStatus, World


class Envelope(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class Success[DataT](Envelope):
    ok: Literal[True]
    data: DataT


class Failure(Envelope):
    ok: Literal[False]
    error: str


type ResponseAdapter[DataT] = TypeAdapter[Success[DataT] | Failure]

ROOM_RESPONSE = TypeAdapter(Success[Room] | Failure)
WORLD_RESPONSE = TypeAdapter(Success[World] | Failure)
SHARDS_RESPONSE = TypeAdapter(Success[tuple[ShardStatus, ...]] | Failure)
PLAYERS_RESPONSE = TypeAdapter(Success[tuple[Player, ...]] | Failure)
PLAYER_RESPONSE = TypeAdapter(Success[Player | None] | Failure)
INVENTORY_RESPONSE = TypeAdapter(Success[Inventory | None] | Failure)
BOOL_RESPONSE = TypeAdapter(Success[bool] | Failure)
INT_RESPONSE = TypeAdapter(Success[int] | Failure)
DRIVER_RESPONSE = TypeAdapter(Success[DriverHealth] | Failure)
RUNTIME_RESPONSE = TypeAdapter(Success[Runtime] | Failure)
MODS_RESPONSE = TypeAdapter(Success[tuple[Mod, ...]] | Failure)
JSON_RESPONSE = TypeAdapter(Success[JsonValue] | Failure)


__all__ = [
    "BOOL_RESPONSE",
    "DRIVER_RESPONSE",
    "INT_RESPONSE",
    "INVENTORY_RESPONSE",
    "JSON_RESPONSE",
    "MODS_RESPONSE",
    "PLAYERS_RESPONSE",
    "PLAYER_RESPONSE",
    "ROOM_RESPONSE",
    "RUNTIME_RESPONSE",
    "SHARDS_RESPONSE",
    "WORLD_RESPONSE",
    "Failure",
    "ResponseAdapter",
]
