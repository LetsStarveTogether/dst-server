import subprocess  # ruff:ignore[suspicious-subprocess-import]
from pathlib import Path

import pytest

from dst_server.game.rpc import (
    JSON_RESPONSE,
    MAX_RESULT_LINE_BYTES,
    RESULT_PREFIX,
    Failure,
    lua_package_path,
    lua_request,
    lua_string,
)

TOO_LARGE = b'DST_SERVER_RESULT|{"ok":false,"error":"RPC result exceeds 64 KiB"}'


def lua_response(body: str, luajit: str) -> bytes:
    root = Path(__file__).parents[1]
    bundled = root / "dst-scripts/scripts/json.lua"
    module = "json" if bundled.is_file() else "json_contract"
    directory = bundled.parent if bundled.is_file() else root / "tests/lua"
    setup = (
        f"package.path={lua_string(f'{directory}/?.lua;')}..package.path;"
        f'json=require("{module}");'
    )
    result = subprocess.run(  # ruff:ignore[subprocess-without-shell-equals-true]
        [luajit, "-e", setup + lua_request(body)],
        capture_output=True,
        timeout=5,
        check=True,
    ).stdout
    assert result.startswith(RESULT_PREFIX.encode())
    assert result.endswith(b"\n")
    return result[len(RESULT_PREFIX) : -1]


@pytest.mark.parametrize("character", [";", "?", "\r", "\n"])
def test_lua_package_path_rejects_control_syntax(character: str) -> None:
    with pytest.raises(ValueError, match="Lua directory"):
        lua_package_path(Path(f"/sdk{character}modules"))


@pytest.mark.parametrize("failure", [False, True], ids=["return", "error"])
@pytest.mark.parametrize("overflow", [False, True], ids=["limit", "overflow"])
def test_lua_result_line_limit(
    failure: bool,
    overflow: bool,
    luajit: str,
) -> None:
    envelope = b'{"ok":false,"error":""}' if failure else b'{"ok":true,"data":""}'
    size = (
        MAX_RESULT_LINE_BYTES - len(RESULT_PREFIX.encode()) - len(envelope) + overflow
    )
    value = f'string.rep("x",{size})'
    body = f"error({value},0)" if failure else f"return {value}"
    line = RESULT_PREFIX.encode() + lua_response(body, luajit)

    assert line == TOO_LARGE if overflow else len(line) == MAX_RESULT_LINE_BYTES


@pytest.mark.parametrize(
    ("body", "message"),
    [
        ("return string.char(0)", "RPC result is not valid JSON"),
        ("return string.char(8)", "RPC result is not valid JSON"),
        ("return string.char(12)", "RPC result is not valid JSON"),
        ("return string.char(255)", "RPC result is not valid JSON"),
        ("error(string.char(255),0)", "RPC result is not valid JSON"),
        (
            (
                "error(setmetatable({}, {__tostring=function() "
                'error("stringify failed",0) end}),0)'
            ),
            "Lua request failed",
        ),
        ("return function() end", None),
        ("return {nested={bad=function() end}}", None),
        ("return {nested={bad=coroutine.create(function() end)}}", None),
        ("return {nested={bad=newproxy(true)}}", None),
        ("return 0/0", None),
        ("return math.huge", None),
        ("return -math.huge", None),
        ('return {[1]="numeric",["1"]="string"}', None),
        ("return {[math.huge]=true}", None),
        ("local value={};value.self=value;return value", None),
        ("return {[1]=true,[3]=true}", None),
    ],
    ids=lambda value: str(value)[:32],
)
def test_lua_rejects_values_without_a_json_wire_representation(
    body: str,
    message: str | None,
    luajit: str,
) -> None:
    envelope = JSON_RESPONSE.validate_json(lua_response(body, luajit), strict=True)

    assert isinstance(envelope, Failure)
    if message is not None:
        assert envelope.error == message
