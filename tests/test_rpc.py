import json
import math
import string
import sys
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock

import pytest
from pydantic import JsonValue

from dst_server.game import GameClient, rpc
from dst_server.telemetry import TelemetrySettings
from dst_server.telemetry.recorder import Recorder
from tests.helpers import run_lua

ROOT = Path(__file__).parents[1]
PREFIX = rpc.RESULT_PREFIX.encode()
SUCCESS_OVERHEAD = len(PREFIX) + len(b'{"ok":true,"data":""}')
SAFE_INTEGER = 2**53 - 1
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
    output = run_lua(setup + rpc.lua_request(body), luajit)
    assert output.startswith(PREFIX)
    assert output.endswith(b"\n")
    line = output[:-1]
    assert len(line) <= rpc.MAX_RESULT_LINE_BYTES
    assert all(byte >= 32 for byte in line), "RPC output must be a single JSON line"
    envelope = json.loads(line[len(PREFIX) :].decode("utf-8"))
    rpc.JSON_RESPONSE.validate_json(line[len(PREFIX) :], strict=True)
    return line, envelope


@pytest.mark.parametrize("character", [";", "?", "\r", "\n"])
def test_lua_package_path_rejects_control_syntax(character: str) -> None:
    with pytest.raises(ValueError, match="Lua directory"):
        rpc.lua_package_path(Path(f"/sdk{character}modules"))


@pytest.mark.parametrize("value", [TEXT, "", "\0" + "123", "\x1f" + "999"])
def test_lua_string_preserves_every_byte(value: str, luajit: str) -> None:
    output = run_lua(f"io.write({rpc.lua_string(value)})", luajit)
    assert output == value.encode()


@pytest.mark.parametrize("value", [chr(0xD800), chr(0xDFFF), "key\udc00value"])
def test_lua_string_rejects_surrogates(value: str) -> None:
    with pytest.raises(ValueError, match="UTF-8"):
        rpc.lua_string(value)


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
    client = {"userid": "KU_TEST", "name": "Test", "lobbycharacter": lobbycharacter}
    if prefab is not None:
        client["prefab"] = prefab
    line, _ = response(
        'return require("dst_server.player_queries").get_players()',
        luajit,
        setup=(
            f"GetPlayerClientTable=function() return {{{rpc.lua_value(client)}}} end;"
            "LookupPlayerInstByUserID=function() return nil end;"
        ),
    )
    result = rpc.PLAYERS_RESPONSE.validate_json(line[len(PREFIX) :])
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
    client = {
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
            f"local client={rpc.lua_value(client)};"
            'local player={userid=client.userid,prefab="wilson",components={},'
            "HasTag=function() return false end,"
            "GetDisplayName=function() return client.name end};"
            "GetPlayerClientTable=function() return "
            f"{rpc.lua_value([] if source == 'display-name' else [client])} end;"
            "LookupPlayerInstByUserID=function() return "
            f"{'nil' if source == 'lobby' else 'player'} end;"
        ),
    )
    if source == "display-name":
        result = rpc.PLAYER_RESPONSE.validate_json(line[len(PREFIX) :])
        assert isinstance(result, rpc.Success)
        assert result.data is not None
        actual = result.data
    else:
        players = rpc.PLAYERS_RESPONSE.validate_json(line[len(PREFIX) :])
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
        f'return require("dst_server.values").text({rpc.lua_string(text)},{budget})',
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
    result = rpc.PLAYER_RESPONSE.validate_json(line[len(PREFIX) :])
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
    source = (ROOT / "dst-scripts/scripts/modindex.lua").read_text()
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
                name, version = {rpc.lua_string(name)}, {rpc.lua_string(version)}
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
    result = rpc.MODS_RESPONSE.validate_json(line[len(PREFIX) :])
    assert isinstance(result, rpc.Success)
    assert result.data[0].name == name
    assert result.data[0].version == version


@pytest.mark.parametrize(
    "value",
    [
        None,
        False,
        True,
        0,
        -1,
        SAFE_INTEGER,
        -SAFE_INTEGER,
        1.2345678901234567,
        sys.float_info.max,
        sys.float_info.min,
        math.ulp(0.0),
        TEXT,
        [],
        {},
        {"n": 0},
        [None, False, 0, [], {}, TEXT],
        {TEXT: [None, False, 0, {"n": 0}], "empty": {}},
        '"); error("injected"); --\nDST_SERVER_RESULT|',
    ],
)
def test_lua_value_round_trips_json_values(value: Any, luajit: str) -> None:
    _, envelope = response(f"return {rpc.lua_value(value)}", luajit)
    assert envelope == {"ok": True, "data": value}
    assert json.dumps(envelope["data"], sort_keys=True) == json.dumps(
        value, sort_keys=True
    )


@pytest.mark.parametrize(
    "value",
    [
        math.nan,
        math.inf,
        -math.inf,
        SAFE_INTEGER + 1,
        -SAFE_INTEGER - 1,
        10**400,
        b"bytes",
        (1, 2),
        {1, 2},
        {1: "numeric key"},
        {False: "boolean key"},
        {None: "null key"},
        {"nested": [math.nan]},
        {"nested": {1: "bad key"}},
        "\ud800",
        {"\udfff": "bad key"},
        object(),
    ],
)
def test_lua_value_rejects_values_outside_its_contract(value: Any) -> None:
    with pytest.raises((TypeError, ValueError)):
        rpc.lua_value(value)


def test_lua_value_rejects_cycles_and_accepts_shared_values(luajit: str) -> None:
    cyclic_list: list[Any] = []
    cyclic_list.append(cyclic_list)
    cyclic_object: dict[str, Any] = {}
    cyclic_object["self"] = cyclic_object
    for cyclic in (cyclic_list, cyclic_object):
        with pytest.raises(ValueError, match=r"cyclic|circular|cycle"):
            rpc.lua_value(cyclic)
    shared = {"value": [None, False]}
    value = [shared, shared]
    _, envelope = response(f"return {rpc.lua_value(value)}", luajit)
    assert envelope == {"ok": True, "data": value}


@pytest.mark.parametrize(
    ("expression", "expected"),
    [
        ("nil", None),
        ("json.null", None),
        ("false", False),
        ("true", True),
        ("0", 0),
        ("{}", []),
        ("wire.object({})", {}),
        ("{n=0}", {"n": 0}),
        ("{json.null,false,0,{}}", [None, False, 0, []]),
        (
            '{text="中文😀",empty=wire.object({}),null=json.null}',
            {"text": "中文😀", "empty": {}, "null": None},
        ),
        ("1.2345678901234567", 1.2345678901234567),
        ("1.7976931348623157e308", sys.float_info.max),
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
    assert json.dumps(json.loads(output.decode("utf-8")), sort_keys=True) == json.dumps(
        expected, sort_keys=True
    )


def test_wire_encoder_escapes_all_controls_in_values_and_keys(luajit: str) -> None:
    literal = rpc.lua_string(TEXT)
    output = run_lua(
        'local wire=require("dst_server.wire");'
        f"io.write(wire.encode({{[{literal}]={literal}}}))",
        luajit,
    )
    assert all(byte >= 32 for byte in output)
    assert json.loads(output.decode("utf-8")) == {TEXT: TEXT}


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
        value = f'{rpc.lua_string(UNICODE_INPUTS["emoji-zwj"])}..{value}.."tail"'
    body = f"return {{[{value}]=true}}" if as_key else f"return {{nested={value}}}"
    _, envelope = response(body, luajit)
    assert envelope == {"ok": False, "error": "invalid_utf8"}


def test_wire_accepts_shared_references_without_mistaking_them_for_cycles(
    luajit: str,
) -> None:
    _, envelope = response("local shared={n=0};return {shared,shared}", luajit)
    assert envelope == {"ok": True, "data": [{"n": 0}, {"n": 0}]}


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


def test_reply_invokes_callback_once_and_never_stringifies_its_error(
    luajit: str,
) -> None:
    output = run_lua(
        'local wire=require("dst_server.wire");local calls,stringifications=0,0;'
        "local failure=setmetatable({}, {__tostring=function() "
        'stringifications=stringifications+1;error("private",0) end});'
        "wire.reply(function() calls=calls+1;error(failure,0) end);"
        "assert(calls==1);assert(stringifications==0)",
        luajit,
    )
    assert output == PREFIX + b'{"ok":false,"error":"lua_error"}\n'


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
def test_reply_limits_encoded_bytes_not_source_character_count(
    value: str,
    luajit: str,
) -> None:
    _, envelope = response(f"return string.rep({value},20000)", luajit)
    assert envelope == {"ok": False, "error": "response_too_large"}


def test_lua_request_delegates_to_the_shared_wire_reply() -> None:
    command = rpc.lua_request("return false")
    assert "dst_server.wire" in command
    assert "reply" in command
    assert "return false" in command
    assert len(command) < 200


@pytest.mark.parametrize("operation", ["install", "request", "reload", "health"])
async def test_game_client_uses_safe_literals_and_bootstraps_the_wire_module(
    operation: str,
    luajit: str,
) -> None:
    module = """
        json.decode=function() error("native decoder used",0) end
        package.preload["dst_server"]=function()
            return {
                install=function(options)
                    assert(options.generation==7)
                    assert(options.nonce=="01ARZ3NDEKTSV4RRFFQ69G5FAV")
                    assert(options.profile=="off")
                    return {protocol=2,generation=options.generation,
                        telemetry_status="disabled",
                        last_error=json.null,events_emitted=0,errors=0}
                end,
                call=function(name,args)
                    if name=="health" then
                        return {protocol=2,generation=3,telemetry_status="active",
                            last_error=json.null,events_emitted=7,errors=0}
                    end
                    assert(name=="echo")
                    return args
                end,
            }
        end
    """
    commands: list[str] = []

    async def execute(command: str) -> str:  # ruff:ignore[unused-async]
        commands.append(command)
        source = module + command
        if operation == "install":
            for prefix in ("", "mod/?.lua;"):
                source += (
                    f";package.path={rpc.lua_string(prefix)}..package.path;"
                    f"local installed_path=package.path;{command};"
                    "assert(package.path==installed_path)"
                )
        return run_lua(source, luajit, driver_path=operation != "install").decode()

    async def reload(command: str, completion_timeout: float) -> tuple[str, int, float]:
        assert completion_timeout == 30
        return await execute(command), 7, 100.0

    wait_reload = AsyncMock()
    observations: list[tuple[int, rpc.DriverHealth]] = []
    game = GameClient(
        shard="Master",
        lua_directory=ROOT / "src/dst_server/lua",
        telemetry=TelemetrySettings(profile="off"),
        execute=execute,
        execute_ready=execute,
        execute_reload=reload,
        wait_reload=wait_reload,
        recorder=Recorder("cluster", "Master"),
        session_id=lambda: "SESSION",
        nonce="01ARZ3NDEKTSV4RRFFQ69G5FAV",
        observe_health=lambda generation, health: observations.append((
            generation,
            health,
        )),
    )
    value: dict[str, JsonValue] = {"text": TEXT, "empty": {}, "items": [None, False, 0]}
    if operation == "install":
        health = await game.install(7)
        assert health.protocol == 2
        assert health.generation == 7
        assert health.telemetry_status == "disabled"
    elif operation == "request":
        assert await game.request("echo", value, rpc.JSON_RESPONSE) == value
    elif operation == "reload":
        assert await game.reload("echo", value, rpc.JSON_RESPONSE, 30) == value
        wait_reload.assert_awaited_once_with(7, 100.0)
    else:
        health = await game.get_health()
        assert health.generation == 3
        assert observations == [(3, health)]
    if operation != "health":
        assert observations == []
    assert len(commands) == 1
    assert "json.decode" not in commands[0]
