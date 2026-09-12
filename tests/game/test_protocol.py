import math
import string
import sys
from typing import Any
from unittest.mock import AsyncMock

import orjson
import pytest
from pydantic import JsonValue

from dst_server import commands as c
from dst_server.game import GameClient, rpc
from dst_server.lua_codec import LuaValue, lua_string, render_literal
from dst_server.models import Mod, Player
from dst_server.models.driver import DriverHealth
from dst_server.telemetry.recorder import Recorder
from tests.helpers import native_scripts, run_lua

SUCCESS_OVERHEAD = len(b'{"ok":true,"data":""}')
TEXT = (
    "".join(chr(code) + string.digits for code in range(32))
    + '\\"中文😀𐀀\u007f\u0080\u07ff\u0800\ud7ff\ue000\uffff\U0010ffff'
    + "\u0085\u2028\u2029"
)
UNICODE_INPUTS = {
    "nfc": "\u00e9",
    "nfd": "e\u0301",
    "emoji-zwj": "\U0001f469\U0001f3fd\u200d\U0001f680",
    "emoji-family": "\U0001f468\u200d\U0001f469\u200d\U0001f467",
    "emoji-flag": "\U0001f1e8\U0001f1f3",
    "emoji-keycap": "1\ufe0f\u20e3",
    "emoji-presentation": "\u2764\ufe0f",
    "text-presentation": "\u2764\ufe0e",
    "zero-width": "A\u200b\u200c\u200d\u2060\ufeffB",
    "bidi": "A\u202eABC\u202c\u2067XYZ\u2069B",
    "unicode-separators": "A\u0085B\u2028C\u2029D",
    # emoji_items.lua: beefalo and abigail use supplementary private-use code points.
    "dst-emoji": "\u6d4b\u8bd5\U000f0001\U000f001c",
}
TEXT += "".join(UNICODE_INPUTS.values())


def response(body: str, luajit: str, *, setup: str = "") -> tuple[bytes, Any]:
    output = run_lua(
        setup
        + 'local wire=require("dst_server.wire");'
        + f'io.write(wire.response(function() {body} end), "\\n")',
        luajit,
    )
    assert output.endswith(b"\n")
    line = output[:-1]
    assert len(line) <= rpc.MAX_RESULT_LINE_BYTES
    assert all(byte >= 32 for byte in line), "RPC output must be a single JSON line"
    envelope = orjson.loads(line)
    rpc.response_adapter(JsonValue).validate_json(line, strict=True)
    return line, envelope


@pytest.mark.parametrize("value", [TEXT, "", "\0" + "123", "\x1f" + "999"])
def test_lua_string_preserves_every_byte(value: str, luajit: str) -> None:
    output = run_lua(f"io.write({lua_string(value)})", luajit)
    assert output == value.encode()


@pytest.mark.parametrize("value", [chr(0xD800), chr(0xDFFF), "key\udc00value"])
def test_lua_string_rejects_surrogates(value: str) -> None:
    with pytest.raises(ValueError, match="UTF-8"):
        lua_string(value)


def test_player_attribution_uses_native_follower_ownership(luajit: str) -> None:
    run_lua(
        """
        require("class")
        local Follower = require("components/follower")
        local values = require("dst_server.values")
        local player = {
            userid = "KU_TEST",
            HasTag = function(_, tag) return tag == "player" end,
        }
        local follower = setmetatable({ leader = {}, itemowner = player }, {
            __index = Follower,
        })
        local pet = { components = { follower = follower } }
        assert(values.player_for(pet) == player)
        follower.itemowner = nil
        follower.leader = player
        assert(values.player_for(pet) == player)
        follower.leader = nil
        assert(values.player_for(pet) == nil)
        """,
        luajit,
    )


@pytest.mark.parametrize(
    ("prefab", "lobbycharacter", "expected"),
    [
        ("", "wilson", "wilson"),
        (None, "", None),
        ("", "", None),
        ("wx78", "wilson", "wx78"),
    ],
)
def test_player_queries_accept_unselected_characters(
    prefab: str | None,
    lobbycharacter: str,
    expected: str | None,
    luajit: str,
) -> None:
    client: dict[str, LuaValue] = {
        "userid": "KU_TEST",
        "name": "Test",
        "lobbycharacter": lobbycharacter,
    }
    if prefab is not None:
        client["prefab"] = prefab
    line, _ = response(
        'return require("dst_server.player_queries").get_players()',
        luajit,
        setup=(
            f"GetPlayerClientTable=function() return {{{render_literal(client)}}} end;"
            "LookupPlayerInstByUserID=function() return nil end;"
        ),
    )
    result = rpc.response_adapter(tuple[Player, ...]).validate_json(line)
    assert isinstance(result, rpc.Success)
    assert result.data[0].prefab == expected


@pytest.mark.parametrize(
    "name",
    [*UNICODE_INPUTS.values(), UNICODE_INPUTS["emoji-zwj"] * 20],
    ids=[*UNICODE_INPUTS, "long-emoji-name"],
)
@pytest.mark.parametrize("source", ["lobby", "entity", "display-name"])
def test_player_names_preserve_unicode_code_points(
    name: str, source: str, luajit: str
) -> None:
    client: dict[str, LuaValue] = {
        "userid": "KU_TEST",
        "name": name,
        "prefab": "" if source == "lobby" else "wilson",
    }
    query = (
        'get_player({userid="KU_TEST"})'
        if source == "display-name"
        else "get_players()"
    )
    line, _ = response(
        f'return require("dst_server.player_queries").{query}',
        luajit,
        setup=(
            f"local client={render_literal(client)};"
            'local player={userid=client.userid,prefab="wilson",components={},'
            "HasTag=function() return false end,"
            "GetDisplayName=function() return client.name end};"
            "GetPlayerClientTable=function() return "
            f"{{{'' if source == 'display-name' else 'client'}}} end;"
            "LookupPlayerInstByUserID=function() return "
            f"{'nil' if source == 'lobby' else 'player'} end;"
        ),
    )
    if source == "display-name":
        result = rpc.response_adapter(Player | None).validate_json(line)
        assert isinstance(result, rpc.Success)
        assert result.data is not None
        actual = result.data
    else:
        players = rpc.response_adapter(tuple[Player, ...]).validate_json(line)
        assert isinstance(players, rpc.Success)
        (actual,) = players.data
    assert (actual.state is None) == (source == "lobby")
    assert actual.name.encode() == name.encode()


@pytest.mark.parametrize("text", UNICODE_INPUTS.values(), ids=UNICODE_INPUTS)
@pytest.mark.parametrize("budget", range(1, 9))
def test_lua_text_limit_keeps_complete_utf8_code_points(
    text: str, budget: int, luajit: str
) -> None:
    _, envelope = response(
        f'return require("dst_server.values").text({lua_string(text)},{budget})',
        luajit,
    )
    assert envelope == {
        "ok": True,
        "data": text.encode()[:budget].decode(errors="ignore"),
    }


def test_player_queries_accept_native_follower_counts(luajit: str) -> None:
    line, _ = response(
        'return require("dst_server.player_queries").get_player({userid="KU_TEST"})',
        luajit,
        setup="""
        require("class")
        local Leader = require("components/leader")
        local function noop() end
        NotifyPlayerProgress = noop
        local player = {
            userid = "KU_TEST", prefab = "wilson", components = {},
            ListenForEvent = noop, HasTag = function() return false end,
        }
        player.components.leader = Leader(player)
        for guid = 1, 257 do
            player.components.leader:AddFollower({
                prefab = "pigman", GUID = guid, PushEvent = noop,
                components = { follower = { SetLeader = noop } },
            })
        end
        assert(player.components.leader:GetNumFollowers() == 257)
        GetPlayerClientTable = function() return {{userid="KU_TEST", name="Test"}} end
        LookupPlayerInstByUserID = function() return player end
        """,
    )
    result = rpc.response_adapter(Player | None).validate_json(line)
    assert isinstance(result, rpc.Success)
    assert result.data is not None
    assert result.data.state is not None
    assert result.data.state.follower_count == 257
    assert len(result.data.state.followers) == 257


@pytest.mark.parametrize(
    ("name", "version"),
    [("N" * 257, "1"), ("Test", "v" * 129)],
    ids=["long-name", "long-version"],
)
def test_mod_queries_accept_native_metadata(
    name: str, version: str, luajit: str
) -> None:
    source = (native_scripts() / "modindex.lua").read_text()
    declaration = "function ModIndex:InitializeModInfo(modname)"
    initializer = (
        declaration
        + source.split(declaration, 1)[1].split(
            "\nfunction ModIndex:GetModActualName", 1
        )[0]
    )
    line, _ = response(
        'return require("dst_server.world_queries").get_mods()',
        luajit,
        setup=f"""
        require("class")
        require("util")
        ModIndex = {{}}
        LOC = {{GetLocaleCode=function() return "en" end}}
        MODS_ROOT, MOD_API_VERSION = "/unused/", 10
        function kleiloadlua()
            return function()
                name, version = {lua_string(name)}, {lua_string(version)}
                description, author = "Test", "Test"
                api_version, dst_compatible = 10, true
            end
        end
        {initializer}
        local info = ModIndex:InitializeModInfo("local-test")
        assert(not info.failed)
        KnownModIndex = {{GetModInfo=function() return info end}}
        ModManager = {{GetEnabledModNames=function() return {{"local-test"}} end}}
        """,
    )
    result = rpc.response_adapter(tuple[Mod, ...]).validate_json(line)
    assert isinstance(result, rpc.Success)
    assert result.data[0].name == name
    assert result.data[0].version == version


@pytest.mark.parametrize(
    ("expression", "expected"),
    [
        ("nil", None),
        ("json.null", None),
        ("false", False),
        ("true", True),
        ("0", 0),
        ("-1", -1),
        ("9007199254740991", 2**53 - 1),
        ("-9007199254740991", -(2**53 - 1)),
        ("{}", []),
        ("wire.object({})", {}),
        ("{n=0}", {"n": 0}),
        ("{json.null,false,0,{},wire.object({})}", [None, False, 0, [], {}]),
        (
            '{text="中文😀",empty=wire.object({}),null=json.null}',
            {"text": "中文😀", "empty": {}, "null": None},
        ),
        ("1.2345678901234567", 1.2345678901234567),
        ("1.7976931348623157e308", sys.float_info.max),
        ("2.2250738585072014e-308", sys.float_info.min),
        ("4.9406564584124654e-324", math.ulp(0.0)),
    ],
)
def test_wire_encoder_preserves_json_types(
    expression: str,
    expected: Any,
    luajit: str,
) -> None:
    output = run_lua(
        f'local wire=require("dst_server.wire");io.write(wire.encode({expression}))',
        luajit,
    )
    assert orjson.dumps(
        orjson.loads(output), option=orjson.OPT_SORT_KEYS
    ) == orjson.dumps(expected, option=orjson.OPT_SORT_KEYS)


def test_wire_encoder_escapes_all_controls_in_values_and_keys(luajit: str) -> None:
    literal = lua_string(TEXT)
    output = run_lua(
        'local wire=require("dst_server.wire");'
        f"io.write(wire.encode({{[{literal}]={literal}}}))",
        luajit,
    )
    assert all(byte >= 32 for byte in output)
    assert orjson.loads(output) == {TEXT: TEXT}


def test_wire_does_not_delegate_to_native_json_codec(luajit: str) -> None:
    _, envelope = response(
        "return {n=0,text=string.char(0),nothing=json.null}",
        luajit,
        setup=(
            'local function forbidden() error("native codec used",0) end;'
            "json.encode=forbidden;json.encode_compliant=forbidden;"
            "json.decode=forbidden;"
        ),
    )
    assert envelope == {"ok": True, "data": {"n": 0, "text": "\0", "nothing": None}}


@pytest.mark.parametrize(
    "body",
    [
        "return 0/0",
        "return math.huge",
        "return -math.huge",
        "return function() end",
        "return coroutine.create(function() end)",
        "return newproxy(true)",
        "return {nested={bad=function() end}}",
        'return {[1]="numeric",["1"]="string"}',
        "return {[math.huge]=true}",
        "return {[true]=true}",
        "return {[{}]=true}",
        "return {[0]=true}",
        "return {[-1]=true}",
        "return {[1.5]=true}",
        "return {[1]=true,[3]=true}",
        "local value={};value.self=value;return value",
        "local a,b={},{};a[1]=b;b[1]=a;return a",
    ],
)
def test_wire_rejects_unsupported_values_with_stable_error(
    body: str,
    luajit: str,
) -> None:
    _, envelope = response(body, luajit)
    assert envelope == {"ok": False, "error": "invalid_json_value"}


@pytest.mark.parametrize(
    "bytes_",
    [
        "128",  # Stray continuation.
        "192,128",  # Overlong two-byte sequence.
        "194",  # Truncated two-byte sequence.
        "194,65",  # Non-continuation second byte.
        "224,128,128",  # Overlong three-byte sequence.
        "237,160,128",  # UTF-16 surrogate.
        "240,128,128,128",  # Overlong four-byte sequence.
        "244,144,128,128",  # Beyond U+10FFFF.
        "245,128,128,128",  # Invalid leading byte.
        "240,144,128",  # Truncated four-byte sequence.
        "255",  # Invalid byte.
    ],
)
@pytest.mark.parametrize("as_key", [False, True], ids=["value", "key"])
@pytest.mark.parametrize("mixed", [False, True], ids=["isolated", "mixed-unicode"])
def test_wire_rejects_invalid_utf8(
    bytes_: str,
    as_key: bool,
    mixed: bool,
    luajit: str,
) -> None:
    value = f"string.char({bytes_})"
    if mixed:
        value = f'{lua_string(UNICODE_INPUTS["emoji-zwj"])}..{value}.."tail"'
    body = f"return {{[{value}]=true}}" if as_key else f"return {{nested={value}}}"
    _, envelope = response(body, luajit)
    assert envelope == {"ok": False, "error": "invalid_utf8"}


def test_wire_accepts_shared_references_without_mistaking_them_for_cycles(
    luajit: str,
) -> None:
    _, envelope = response(
        "local shared={value={json.null,false}};return {shared,shared}", luajit
    )
    assert envelope == {
        "ok": True,
        "data": [{"value": [None, False]}, {"value": [None, False]}],
    }


@pytest.mark.parametrize(
    "error",
    [
        '"chat=private token=SECRET source=/private/mod.lua\\nsecond line"',
        '"invalid_utf8"',
        '"invalid_json_value"',
        '"response_too_large"',
        'string.rep("secret",100000)',
        "string.char(255)",
        "{}",
        "false",
        "setmetatable({}, {__tostring=function() error('secret',0) end})",
    ],
)
def test_callback_errors_are_private_and_have_a_stable_category(
    error: str,
    luajit: str,
) -> None:
    _, envelope = response(f"error({error},0)", luajit)
    assert envelope == {"ok": False, "error": "lua_error"}


def test_response_invokes_callback_once_and_never_stringifies_its_error(
    luajit: str,
) -> None:
    output = run_lua(
        'local wire=require("dst_server.wire");local calls,stringifications=0,0;'
        "local failure=setmetatable({}, {__tostring=function() "
        'stringifications=stringifications+1;error("private",0) end});'
        'io.write(wire.response(function() calls=calls+1;error(failure,0) end), "\\n");'
        "assert(calls==1);assert(stringifications==0)",
        luajit,
    )
    assert output == b'{"ok":false,"error":"lua_error"}\n'


@pytest.mark.parametrize("overflow", [False, True], ids=["limit", "overflow"])
def test_lua_result_line_limit(overflow: bool, luajit: str) -> None:
    size = rpc.MAX_RESULT_LINE_BYTES - SUCCESS_OVERHEAD + overflow
    line, envelope = response(f'return string.rep("x",{size})', luajit)
    if overflow:
        assert envelope == {"ok": False, "error": "response_too_large"}
    else:
        assert len(line) == rpc.MAX_RESULT_LINE_BYTES
        assert envelope == {"ok": True, "data": "x" * size}


@pytest.mark.parametrize("value", ["string.char(0)", '"😀"'])
def test_response_limits_encoded_bytes_not_source_character_count(
    value: str,
    luajit: str,
) -> None:
    _, envelope = response(f"return string.rep({value},20000)", luajit)
    assert envelope == {"ok": False, "error": "response_too_large"}


@pytest.mark.parametrize(
    "source",
    [
        'local value=string.rep("x", 4*1024*1024)',
        "local value=string.rep(string.char(0), 65000)",
        'local value={}; for index=1,10000 do value[index]=string.rep("x", 1024) end',
    ],
    ids=["large-string", "escaped-string", "large-array"],
)
def test_wire_stops_encoding_when_the_byte_budget_is_exhausted(
    source: str, luajit: str
) -> None:
    run_lua(
        f"""
        local wire=require("dst_server.wire")
        {source}
        collectgarbage("collect")
        collectgarbage("stop")
        local baseline=collectgarbage("count")
        local ok, failure=pcall(wire.encode, value, 65000)
        local allocated=collectgarbage("count")-baseline
        assert(not ok and failure=="response_too_large")
        assert(allocated < 512, "encoder retained "..allocated.." KiB")
        collectgarbage("restart")
        """,
        luajit,
    )


@pytest.mark.parametrize("operation", ["request", "reload", "health"])
async def test_game_client_sends_typed_method_and_arguments(operation: str) -> None:
    commands: list[tuple[str, dict[str, JsonValue]]] = []

    async def execute(method: str, arguments: dict[str, JsonValue]) -> bytes:  # ruff:ignore[unused-async]
        commands.append((method, arguments))
        data = (
            {
                "protocol": 2,
                "generation": 3,
                "telemetry_status": "active",
                "last_error": None,
                "events_emitted": 7,
                "errors": 0,
            }
            if method == "health"
            else arguments
        )
        return orjson.dumps({"ok": True, "data": data})

    async def reload(
        method: str, arguments: dict[str, JsonValue], completion_timeout: float
    ) -> tuple[bytes, int, float]:
        assert completion_timeout == 30
        return await execute(method, arguments), 7, 100.0

    wait_reload = AsyncMock()
    observations: list[tuple[int, DriverHealth]] = []
    game = GameClient(
        shard="Master",
        execute_ready=execute,
        execute_reload=reload,
        wait_reload=wait_reload,
        recorder=Recorder("cluster", "Master"),
        session_id=lambda: "SESSION",
        observe_health=lambda generation, health: observations.append((
            generation,
            health,
        )),
    )
    value: dict[str, JsonValue] = {"text": TEXT, "empty": {}, "items": [None, False, 0]}
    if operation == "request":
        assert (
            await game.request("echo", value, rpc.response_adapter(JsonValue)) == value
        )
    elif operation == "reload":
        assert (
            await game.reload("echo", value, rpc.response_adapter(JsonValue), 30)
            == value
        )
        wait_reload.assert_awaited_once_with(7, 100.0)
    else:
        health = await game.invoke(c.Health())
        assert health.generation == 3
        assert observations == [(3, health)]
    if operation != "health":
        assert observations == []
    assert commands == (
        [("health", {})] if operation == "health" else [("echo", value)]
    )
