from __future__ import annotations

import json
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, JsonValue, TypeAdapter

from dst_server.models import Inventory, Mod, Player, Room, Runtime, ShardStatus, World
from dst_server.models.base import FrozenModel, Identifier, NonNegativeInt

RESULT_PREFIX = "DST_SERVER_RESULT|"
MAX_RESULT_LINE_BYTES = 64 * 1024


class DriverHealth(FrozenModel):
    protocol: Literal[1]
    telemetry_status: Literal["disabled", "active", "failed"]
    telemetry_error: str | None
    events_emitted: NonNegativeInt
    errors: NonNegativeInt


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
PLAYER_IDS_RESPONSE = TypeAdapter(Success[tuple[Identifier, ...]] | Failure)
JSON_RESPONSE = TypeAdapter(Success[JsonValue] | Failure)


def lua_request(body: str) -> str:
    prefix = lua_string(RESULT_PREFIX)
    failed = lua_string(json_text({"ok": False, "error": "Lua request failed"}))
    invalid = lua_string(
        json_text({"ok": False, "error": "RPC result is not valid JSON"})
    )
    success = lua_string('{"ok":true,"data":')
    too_large = lua_string(
        json_text({"ok": False, "error": "RPC result exceeds 64 KiB"})
    )
    return (
        "local function valid_json_value(value,seen) "
        "if value==nil or value==json.null then return true end;"
        "local kind=type(value);"
        'if kind=="boolean" or kind=="string" then return true end;'
        'if kind=="number" then return value==value and value~=math.huge '
        "and value~=-math.huge end;"
        'if kind~="table" or seen[value] then return false end;seen[value]=true;'
        "local mode,count,maximum=nil,0,0;"
        "for key,item in pairs(value) do local key_mode;local key_kind=type(key);"
        'if key_kind=="string" then key_mode="object";'
        'elseif key_kind=="number" and key==key and key~=math.huge '
        "and key~=-math.huge and key>=1 and key==math.floor(key) then "
        'key_mode="array";count=count+1;maximum=math.max(maximum,key);'
        "else seen[value]=nil;return false end;"
        "if mode~=nil and mode~=key_mode then seen[value]=nil;return false end;"
        "mode=key_mode;"
        "if not valid_json_value(item,seen) then seen[value]=nil;return false end end;"
        'seen[value]=nil;return mode~="array" or count==maximum end;'
        "local function valid_utf8(value) local index=1;"
        "while index<=#value do local byte=string.byte(value,index);local width;"
        "if byte<128 then width=0;"
        "elseif byte>=194 and byte<=223 then width=1;"
        "elseif byte>=224 and byte<=239 then width=2;"
        "elseif byte>=240 and byte<=244 then width=3;else return false end;"
        "if index+width>#value then return false end;"
        "local second=string.byte(value,index+1);"
        "if (byte==224 and second<160) or (byte==237 and second>159) "
        "or (byte==240 and second<144) or (byte==244 and second>143) "
        "then return false end;"
        "for offset=1,width do local continuation=string.byte(value,index+offset);"
        "if continuation<128 or continuation>191 then return false end end;"
        "index=index+width+1 end;return true end;"
        "local function failure(value) local text_ok,text=pcall(tostring,value);"
        'if not text_ok or type(text)~="string" then text="Lua request failed" end;'
        'text=string.gsub(text,"[%z\\1-\\31]"," ");'
        "local encode_ok,encoded=pcall(json.encode_compliant,"
        "{ok=false,error=text});"
        f'return encode_ok and type(encoded)=="string" and encoded or {failed} end;'
        "local ok,payload=pcall(function() local data=(function() "
        f"{body} end)();if data==nil then data=json.null end;"
        "if not valid_json_value(data,{}) then "
        'error("RPC result is not a JSON value",0) end;'
        "local encoded=json.encode_compliant(data);"
        'if type(encoded)~="string" then error("JSON encoder returned non-string") end;'
        f'return {success}..encoded.."}}" end);'
        "if not ok then payload=failure(payload) end;"
        'if type(payload)~="string" or string.find(payload,"[%z\\1-\\31]") '
        f"or not valid_utf8(payload) then payload={invalid} end;"
        f"local prefix={prefix};"
        f"if #prefix+#payload>{MAX_RESULT_LINE_BYTES} then payload={too_large} end;"
        "print(prefix..payload)"
    )


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
    "MAX_RESULT_LINE_BYTES",
    "MODS_RESPONSE",
    "PLAYERS_RESPONSE",
    "PLAYER_IDS_RESPONSE",
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
