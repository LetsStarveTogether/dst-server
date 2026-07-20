from __future__ import annotations

import json
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, JsonValue, TypeAdapter

from dst_server.models import Inventory, Mod, Player, Room, Runtime, ShardStatus, World
from dst_server.models.base import FrozenModel, NonNegativeInt
from dst_server.telemetry import TelemetryProfile

RESULT_PREFIX = "DST_SERVER_RESULT|"


class DriverHealth(FrozenModel):
    protocol: Literal[1]
    installed: bool
    profile: TelemetryProfile
    events_emitted: NonNegativeInt
    errors: NonNegativeInt
    players: NonNegativeInt
    action_hook: bool
    shard_hook: bool


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


def lua_request(body: str) -> str:
    return (
        "local ok,payload=pcall(function() local data=(function() "
        f"{body} end)();if data==nil then data=json.null end;"
        "return json.encode_compliant({ok=true,data=data}) end);"
        "if not ok then "
        "payload=json.encode_compliant({ok=false,error=tostring(payload)}) end;"
        f"print({lua_string(RESULT_PREFIX)}..payload)"
    )


def lua_string(value: str) -> str:
    escaped = []
    for character in value:
        if character == '"':
            escaped.append('\\"')
        elif character == "\\":
            escaped.append("\\\\")
        elif character.isprintable():
            escaped.append(character)
        else:
            escaped.extend(f"\\{byte:03d}" for byte in character.encode())
    return f'"{"".join(escaped)}"'


def json_text(value: JsonValue) -> str:
    return json.dumps(
        value,
        allow_nan=False,
        ensure_ascii=False,
        separators=(",", ":"),
    )


def lua_package_path(directory: Path) -> str:
    value = str(directory)
    if any(character in value for character in ";?\r\n"):
        msg = "Lua directory must not contain ';', '?', CR, or LF"
        raise ValueError(msg)
    return f"{value}/?.lua;"


__all__ = [
    "BOOL_RESPONSE",
    "DRIVER_RESPONSE",
    "INT_RESPONSE",
    "INVENTORY_RESPONSE",
    "JSON_RESPONSE",
    "MODS_RESPONSE",
    "PLAYERS_RESPONSE",
    "PLAYER_RESPONSE",
    "RESULT_PREFIX",
    "ROOM_RESPONSE",
    "RUNTIME_RESPONSE",
    "SHARDS_RESPONSE",
    "WORLD_RESPONSE",
    "DriverHealth",
    "Failure",
    "ResponseAdapter",
    "json_text",
    "lua_package_path",
    "lua_request",
    "lua_string",
]
