import json
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, JsonValue, TypeAdapter

from dst_server.events.base import DriverDiagnostic
from dst_server.models import Inventory, Mod, Player, Room, Runtime, ShardStatus, World
from dst_server.models.base import FrozenModel, Identifier, NonNegativeInt

RESULT_PREFIX = "DST_SERVER_RESULT|"
MAX_RESULT_LINE_BYTES = 64 * 1024
MAX_SAFE_INTEGER = 2**53 - 1


class DriverHealth(FrozenModel):
    protocol: Literal[2]
    generation: NonNegativeInt
    telemetry_status: Literal["disabled", "active", "degraded", "failed"]
    last_error: DriverDiagnostic | None
    events_emitted: NonNegativeInt
    errors: NonNegativeInt


class Envelope(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class Success[DataT](Envelope):
    ok: Literal[True]
    data: DataT


class Failure(Envelope):
    ok: Literal[False]
    error: Literal[
        "lua_error", "invalid_json_value", "invalid_utf8", "response_too_large"
    ]


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
PLAYER_IDS_RESPONSE = TypeAdapter(Success[tuple[Identifier, ...]] | Failure)
JSON_RESPONSE = TypeAdapter(Success[JsonValue] | Failure)


def lua_request(body: str) -> str:
    return f'require("dst_server.wire").reply(function() {body} end)'


def lua_string(value: str) -> str:
    try:
        value.encode("utf-8")
    except UnicodeEncodeError as error:
        msg = "Lua strings must contain valid UTF-8"
        raise ValueError(msg) from error
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


def lua_value(value: JsonValue) -> str:
    return _lua_value(value, set())


def _lua_value(value: JsonValue, seen: set[int]) -> str:
    if value is None:
        return 'require("json").null'
    if isinstance(value, str):
        return lua_string(value)
    if isinstance(value, (bool, int, float)):
        if isinstance(value, int) and abs(value) > MAX_SAFE_INTEGER:
            msg = "Lua integers must be within the exact IEEE 754 integer range"
            raise ValueError(msg)
        return json.dumps(value, allow_nan=False)
    if not isinstance(value, (list, dict)):
        msg = "Lua values must be JSON values"
        raise TypeError(msg)
    identity = id(value)
    if identity in seen:
        msg = "Lua value contains a cycle"
        raise ValueError(msg)
    seen.add(identity)
    try:
        if isinstance(value, list):
            return "{" + ",".join(_lua_value(item, seen) for item in value) + "}"
        if any(not isinstance(key, str) for key in value):
            msg = "Lua object keys must be strings"
            raise TypeError(msg)
        if not value:
            return 'require("dst_server.wire").object({})'
        return (
            "{"
            + ",".join(
                f"[{lua_string(key)}]={_lua_value(item, seen)}"
                for key, item in value.items()
            )
            + "}"
        )
    finally:
        seen.remove(identity)


def lua_package_path(directory: Path) -> str:
    value = str(directory)
    if any(character in value for character in ";?\r\n"):
        msg = "Lua directory must not contain ';', '?', CR, or LF"
        raise ValueError(msg)
    return f"{value}/?.lua;"
