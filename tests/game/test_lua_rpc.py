from pathlib import Path

import orjson
import pytest
from hypothesis import given
from hypothesis import strategies as st
from pydantic import JsonValue

from tests.lua.helpers import run_lua_process

NONCE = "01ARZ3NDEKTSV4RRFFQ69G5FAV"
IDENTIFIER = "01ARZ3NDEKTSV4RRFFQ69G5FAW"
PREFIX = b"DST_RPC|"
LIMIT = 64 * 1024
JSON_VALUES = st.recursive(
    st.none()
    | st.booleans()
    | st.integers(-(2**53) + 1, 2**53 - 1)
    | st.floats(allow_nan=False, allow_infinity=False)
    | st.text(st.characters(exclude_categories=("Cs",)), max_size=32),
    lambda children: (
        st.lists(children, max_size=4)
        | st.dictionaries(st.text(max_size=16), children, max_size=4)
    ),
    max_leaves=16,
)


def request(**changes: object) -> dict[str, object]:
    return {
        "v": 1,
        "nonce": NONCE,
        "id": IDENTIFIER,
        "generation": 7,
        "method": "ping",
        "arguments": {},
    } | changes


def run_rpc(
    native_scripts: Path, payload: bytes, lua_runtime: str, *, scenario: str = "ready"
) -> list[dict[str, object]]:
    root = Path(__file__).parents[2]
    output = run_lua_process(
        lua_runtime,
        root / "tests/lua/rpc_spec.lua",
        root,
        scenario,
        native_scripts,
        input=payload,
    )
    records = []
    for line in output.splitlines(keepends=True):
        assert line.startswith(PREFIX)
        assert len(line) <= LIMIT
        assert b"SECRET_TOKEN" not in line
        assert b"private chat" not in line
        records.append(orjson.loads(line[len(PREFIX) :]))
    return records


def invoke(
    native_scripts: Path, lua_runtime: str, **changes: object
) -> list[dict[str, object]]:
    return run_rpc(
        native_scripts, PREFIX + orjson.dumps(request(**changes)) + b"\n", lua_runtime
    )


@given(value=JSON_VALUES)
def test_strict_decoder_round_trips_json_without_lua_compilation(
    native_scripts: Path, value: JsonValue, lua_runtime: str
) -> None:
    records = invoke(
        native_scripts, lua_runtime, method="echo", arguments={"value": value}
    )
    assert records[-1]["result"] == {"ok": True, "data": {"value": value}}


@pytest.mark.parametrize("scenario", ["ready", "changed_install"])
def test_lua_rpc_has_one_acceptance_and_correlated_result(
    native_scripts: Path, scenario: str, lua_runtime: str
) -> None:
    records = run_rpc(
        native_scripts, PREFIX + orjson.dumps(request()), lua_runtime, scenario=scenario
    )
    header = {"v": 1, "nonce": NONCE, "id": IDENTIFIER, "generation": 7}
    assert records == [
        header | {"accepted": True},
        header | {"result": {"ok": True, "data": True}},
    ]


def test_lua_rpc_preserves_native_console(
    native_scripts: Path, lua_runtime: str
) -> None:
    assert run_rpc(native_scripts, b"", lua_runtime, scenario="native") == []


def test_lua_rpc_keeps_business_prints_out_of_native_control_pipe(
    native_scripts: Path,
    lua_runtime: str,
) -> None:
    records = run_rpc(
        native_scripts,
        PREFIX + orjson.dumps(request(method="noise")),
        lua_runtime,
        scenario="noise",
    )
    assert len(records) == 2
    assert records[0]["accepted"] is True
    assert records[1]["result"] == {"ok": True, "data": True}


@pytest.mark.parametrize(
    ("scenario", "result"),
    [
        ("ready", {"ok": True, "data": {"snapshot": "session/snapshot"}}),
        ("save_then_error", {"ok": True, "data": {"snapshot": "session/snapshot"}}),
        ("save_start_error", {"ok": False, "error": "lua_error"}),
        ("save_start_indeterminate", {"ok": False, "error": "indeterminate"}),
        ("save_invalid_value", {"ok": False, "error": "invalid_json_value"}),
        ("save_indeterminate", {"ok": False, "error": "indeterminate"}),
    ],
)
def test_lua_rpc_save_correlates_its_deferred_completion(
    native_scripts: Path, scenario: str, result: dict[str, object], lua_runtime: str
) -> None:
    records = run_rpc(
        native_scripts,
        PREFIX + orjson.dumps(request(method="save")),
        lua_runtime,
        scenario=scenario,
    )
    assert len(records) == 2
    assert records[0]["accepted"] is True
    assert records[1]["result"] == result


@pytest.mark.parametrize(
    ("changes", "error"),
    [
        ({"v": 2}, "invalid_request"),
        ({"nonce": IDENTIFIER}, "invalid_request"),
        ({"id": "SECRET_TOKEN private chat"}, "invalid_request"),
        ({"generation": True}, "invalid_request"),
        ({"generation": 0.5}, "invalid_request"),
        ({"generation": 2**53}, "invalid_request"),
        ({"generation": 6}, "stale_generation"),
        ({"generation": 8}, "stale_generation"),
        ({"method": ""}, "invalid_request"),
        ({"method": "return os.execute('private chat')"}, "invalid_request"),
        ({"arguments": []}, "invalid_request"),
        ({"arguments": None}, "invalid_request"),
        ({"extra": "SECRET_TOKEN private chat"}, "invalid_request"),
    ],
)
def test_lua_rpc_rejects_before_acceptance(
    native_scripts: Path, changes: dict[str, object], error: str, lua_runtime: str
) -> None:
    records = invoke(native_scripts, lua_runtime, **changes)
    assert len(records) == 1
    assert records[0]["result"] == {"ok": False, "error": error}
    assert "accepted" not in records[0]


@pytest.mark.parametrize(
    "payload",
    [
        b"null",
        b"[]",
        b"{}",
        b"SECRET_TOKEN private chat",
        b'{"v":1+2}',
        b"{'v':1}",
        b'{"v":1,/* comment */"id":2}',
        b'{"v":1,"v":1}',
        b'{"v":null,"v":1}',
        b'{"v":1,}',
        b'{"v":01}',
        b'{"v":+1}',
        b'{"v":1.}',
        b'{"v":1e}',
        b'{"v":1e999}',
        b'{"v":NaN}',
        b'{"v":"\\x41"}',
        b'{"v":"\\ud800"}',
        b'{"v":"\\udc00"}',
        b'{"v":"\\ud800\\u0000"}',
        b'{"v":"\xff"}',
        b'{"v":"\x00"}',
        b'{"v":true false}',
        b'{"v":1} trailing',
        b"[" * 65 + b"0" + b"]" * 65,
    ],
)
def test_lua_rpc_rejects_non_json_without_evaluation(
    native_scripts: Path, payload: bytes, lua_runtime: str
) -> None:
    records = run_rpc(native_scripts, PREFIX + payload, lua_runtime)
    assert len(records) == 1
    assert records[0]["id"] is None
    assert records[0]["result"] == {"ok": False, "error": "invalid_request"}


@pytest.mark.parametrize(
    ("method", "error"),
    [
        ("throw", "lua_error"),
        ("invalid_utf8", "invalid_utf8"),
        ("invalid_value", "invalid_json_value"),
        ("indeterminate", "indeterminate"),
    ],
)
def test_lua_rpc_returns_safe_execution_errors(
    native_scripts: Path, method: str, error: str, lua_runtime: str
) -> None:
    accepted, result = invoke(native_scripts, lua_runtime, method=method)
    assert accepted["accepted"] is True
    assert result["result"] == {"ok": False, "error": error}


def test_lua_rpc_preserves_json_values_and_unicode(
    native_scripts: Path, lua_runtime: str
) -> None:
    values = {
        "null": None,
        "empty": {},
        "array": [None, {}, [], False],
        "text": '你好👩🏽‍💻\x00\n\t"\\',
        "number": -0.25e3,
    }
    payload = orjson.dumps(request(method="echo", arguments=values)).replace(
        "你好👩🏽‍💻".encode(),
        rb"\u4f60\u597d\ud83d\udc69\ud83c\udffd\u200d\ud83d\udcbb",
    )
    records = run_rpc(native_scripts, PREFIX + payload, lua_runtime)
    assert records[-1]["result"] == {"ok": True, "data": values}
    assert invoke(native_scripts, lua_runtime, method="nothing")[-1]["result"] == {
        "ok": True,
        "data": None,
    }


@pytest.mark.parametrize("excess", [0, 1])
def test_lua_rpc_response_limit_includes_envelope_and_newline(
    native_scripts: Path, excess: int, lua_runtime: str
) -> None:
    empty = {
        "v": 1,
        "nonce": NONCE,
        "id": IDENTIFIER,
        "generation": 7,
        "result": {"ok": True, "data": ""},
    }
    size = LIMIT - len(PREFIX + orjson.dumps(empty) + b"\n")
    records = invoke(
        native_scripts, lua_runtime, method="large", arguments={"size": size + excess}
    )
    if excess:
        assert records[-1]["result"] == {"ok": False, "error": "response_too_large"}
    else:
        assert records[-1]["result"] == {"ok": True, "data": "x" * size}
        assert len(PREFIX + orjson.dumps(records[-1]) + b"\n") == LIMIT


@pytest.mark.parametrize("excess", [0, 1])
def test_lua_rpc_request_limit_includes_prefix_and_newline(
    native_scripts: Path, excess: int, lua_runtime: str
) -> None:
    size = 4096 - len(PREFIX + orjson.dumps(request(arguments={"pad": ""})) + b"\n")
    records = invoke(
        native_scripts, lua_runtime, arguments={"pad": "x" * (size + excess)}
    )
    assert records[-1]["result"] == (
        {"ok": False, "error": "invalid_request"}
        if excess
        else {"ok": True, "data": True}
    )


@pytest.mark.parametrize(
    ("scenario", "error"), [("not_ready", "not_ready"), ("write_error", "lua_error")]
)
def test_lua_rpc_never_executes_before_successful_acceptance(
    native_scripts: Path, scenario: str, error: str, lua_runtime: str
) -> None:
    records = run_rpc(
        native_scripts, PREFIX + orjson.dumps(request()), lua_runtime, scenario=scenario
    )
    assert len(records) == 1
    assert records[0]["result"] == {"ok": False, "error": error}
