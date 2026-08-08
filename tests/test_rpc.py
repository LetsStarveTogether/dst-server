from __future__ import annotations

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


@pytest.mark.parametrize(
    "character",
    [";", "?", "\r", "\n"],
    ids=["semicolon", "question-mark", "carriage-return", "line-feed"],
)
def test_lua_package_path_rejects_control_syntax(character: str) -> None:
    with pytest.raises(ValueError, match="Lua directory"):
        lua_package_path(Path(f"/sdk{character}modules"))


def contract_response(body: str, luajit: str) -> bytes:
    root = Path(__file__).parents[1]
    dst_json = root / "dst-scripts/scripts/json.lua"
    module = "json" if dst_json.is_file() else "json_contract"
    directory = dst_json.parent if dst_json.is_file() else root / "tests/lua"
    package_path = lua_string(f"{directory}/?.lua;")
    setup = f'package.path={package_path}..package.path;json=require("{module}");'
    result = subprocess.run(  # ruff:ignore[subprocess-without-shell-equals-true]
        [luajit, "-e", setup + lua_request(body)],
        capture_output=True,
        timeout=5,
        check=True,
    )
    assert result.stdout.startswith(RESULT_PREFIX.encode())
    assert result.stdout.endswith(b"\n")
    return result.stdout[len(RESULT_PREFIX) : -1]


@pytest.mark.parametrize("fails", [False, True])
@pytest.mark.parametrize("extra", [0, 1])
def test_lua_request_limits_complete_result_line(
    fails: bool,
    extra: int,
    luajit: str,
) -> None:
    envelope_size = len(
        b'{"ok":false,"error":""}' if fails else b'{"ok":true,"data":""}'
    )
    payload_size = (
        MAX_RESULT_LINE_BYTES - len(RESULT_PREFIX.encode()) - envelope_size + extra
    )
    payload = f'string.rep("x",{payload_size})'
    body = f"error({payload},0)" if fails else f"return {payload}"
    line = RESULT_PREFIX.encode() + contract_response(body, luajit)
    if extra:
        assert line == TOO_LARGE
    else:
        assert len(line) == MAX_RESULT_LINE_BYTES
        assert line.startswith(RESULT_PREFIX.encode())


@pytest.mark.parametrize("control", [0, 8, 12])
def test_lua_request_rejects_invalid_json_from_dst_encoder(
    control: int,
    luajit: str,
) -> None:
    envelope = JSON_RESPONSE.validate_json(
        contract_response(f"return string.char({control})", luajit),
        strict=True,
    )

    assert isinstance(envelope, Failure)
    assert envelope.error == "RPC result is not valid JSON"


def test_lua_request_handles_unstringifiable_errors(luajit: str) -> None:
    body = (
        "error(setmetatable({}, {__tostring=function() "
        'error("stringify failed",0) end}),0)'
    )
    envelope = JSON_RESPONSE.validate_json(
        contract_response(body, luajit),
        strict=True,
    )

    assert isinstance(envelope, Failure)
    assert envelope.error == "Lua request failed"


def test_lua_request_rejects_results_omitted_by_dst_encoder(luajit: str) -> None:
    envelope = JSON_RESPONSE.validate_json(
        contract_response("return function() end", luajit),
        strict=True,
    )

    assert isinstance(envelope, Failure)


@pytest.mark.parametrize("fails", [False, True])
def test_lua_request_rejects_invalid_utf8(fails: bool, luajit: str) -> None:
    value = "string.char(255)"
    body = f"error({value},0)" if fails else f"return {value}"
    envelope = JSON_RESPONSE.validate_json(
        contract_response(body, luajit),
        strict=True,
    )

    assert isinstance(envelope, Failure)
    assert envelope.error == "RPC result is not valid JSON"


@pytest.mark.parametrize(
    "body",
    [
        "return {nested={bad=function() end}}",
        "return {nested={bad=coroutine.create(function() end)}}",
        "return {nested={bad=newproxy(true)}}",
        "return 0/0",
        "return math.huge",
        "return -math.huge",
        'return {[1]="numeric",["1"]="string"}',
        "return {[math.huge]=true}",
        "local value={};value.self=value;return value",
        "return {[1]=true,[3]=true}",
    ],
    ids=(
        "nested-function",
        "nested-thread",
        "nested-userdata",
        "nan",
        "positive-infinity",
        "negative-infinity",
        "mixed-keys",
        "infinite-key",
        "cycle",
        "sparse-array",
    ),
)
def test_lua_request_rejects_non_json_values(body: str, luajit: str) -> None:
    envelope = JSON_RESPONSE.validate_json(
        contract_response(body, luajit),
        strict=True,
    )

    assert isinstance(envelope, Failure)
