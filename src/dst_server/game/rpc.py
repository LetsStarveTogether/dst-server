from functools import cache
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, JsonValue, TypeAdapter

from dst_server.models import Inventory, Mod, Player, Room, Runtime, ShardStatus, World
from dst_server.models.base import Identifier
from dst_server.models.driver import DriverHealth
from dst_server.models.snapshot import SnapshotCatalog

RESULT_PREFIX = "DST_SERVER_RESULT|"
MAX_RESULT_LINE_BYTES = 64 * 1024
MAX_SAFE_INTEGER = 2**53 - 1


class Envelope(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class Success[DataT](Envelope):
    ok: Literal[True]
    data: DataT


class Failure(Envelope):
    ok: Literal[False]
    error: Literal[
        "lua_error",
        "invalid_json_value",
        "invalid_utf8",
        "response_too_large",
        "indeterminate",
    ]


type ResponseAdapter[DataT] = TypeAdapter[Success[DataT] | Failure]


@cache
def response_adapter(result_type: Any) -> ResponseAdapter[Any]:
    return TypeAdapter(Success[result_type] | Failure)


class LuaRequestError(RuntimeError):
    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(f"DST Lua request failed: {code}")


ROOM_RESPONSE = response_adapter(Room)
WORLD_RESPONSE = response_adapter(World)
SHARDS_RESPONSE = response_adapter(tuple[ShardStatus, ...])
PLAYERS_RESPONSE = response_adapter(tuple[Player, ...])
PLAYER_RESPONSE = response_adapter(Player | None)
INVENTORY_RESPONSE = response_adapter(Inventory | None)
BOOL_RESPONSE = response_adapter(bool)
INT_RESPONSE = response_adapter(int)
DRIVER_RESPONSE = response_adapter(DriverHealth)
RUNTIME_RESPONSE = response_adapter(Runtime)
SNAPSHOTS_RESPONSE = response_adapter(SnapshotCatalog)
MODS_RESPONSE = response_adapter(tuple[Mod, ...])
PLAYER_IDS_RESPONSE = response_adapter(tuple[Identifier, ...])
JSON_RESPONSE = response_adapter(JsonValue)


def lua_request(body: str) -> str:
    return f'require("dst_server.wire").reply(function() {body} end)'


def lua_package_path(directory: Path) -> str:
    value = str(directory)
    if any(character in value for character in ";?\r\n"):
        msg = "Lua directory must not contain ';', '?', CR, or LF"
        raise ValueError(msg)
    return f"{value}/?.lua;"
