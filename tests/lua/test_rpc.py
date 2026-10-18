"""Lua RPC framing and results at the native console boundary."""

import json
from collections.abc import Callable
from pathlib import Path

import pytest

NONCE = "01ARZ3NDEKTSV4RRFFQ69G5FAV"
IDENTIFIER = "01ARZ3NDEKTSV4RRFFQ69G5FAW"
HEADER = {"v": 1, "nonce": NONCE, "id": IDENTIFIER, "generation": 7}
REQUEST = HEADER | {"method": "ping", "arguments": {}}


def encode(value: object) -> bytes:
    return json.dumps(value, separators=(",", ":")).encode()


@pytest.fixture
def send(
    lua: Callable[..., bytes], root: Path, scripts: Path
) -> Callable[..., list[dict]]:
    def call(
        changes: dict[str, object] | None = None,
        *,
        scenario: str = "ready",
        payload: bytes | None = None,
    ) -> list[dict]:
        output = lua(
            root / "tests/lua/rpc_spec.lua",
            root,
            scenario,
            scripts,
            source=b"DST_RPC|"
            + (encode(REQUEST | (changes or {})) if payload is None else payload)
            + b"\n",
        )
        records = []
        for line in output.splitlines(keepends=True):
            assert line.startswith(b"DST_RPC|")
            assert len(line) <= 65536
            assert b"SECRET_TOKEN" not in line
            assert b"private chat" not in line
            records.append(json.loads(line.removeprefix(b"DST_RPC|")))
        return records

    return call


@pytest.mark.parametrize("scenario", ["ready", "changed_install", "noise"])
def test_accepted_call_has_one_final_reply(
    send: Callable[..., list[dict]], scenario: str
) -> None:
    records = send(
        {"method": "noise" if scenario == "noise" else "ping"}, scenario=scenario
    )
    assert records == [
        HEADER | {"accepted": True},
        HEADER | {"result": {"ok": True, "data": True}},
    ]


def test_save_result(send: Callable[..., list[dict]]) -> None:
    assert send({"method": "save"})[-1]["result"] == {"ok": True, "data": True}


def test_native_commands_keep_their_return_values(
    lua: Callable[..., bytes], root: Path, scripts: Path
) -> None:
    assert lua(root / "tests/lua/rpc_spec.lua", root, "native", scripts) == b""


@pytest.mark.parametrize(
    ("changes", "error"),
    [
        pytest.param({"v": 3}, "invalid_request", id="version"),
        pytest.param({"nonce": IDENTIFIER}, "invalid_request", id="nonce"),
        pytest.param({"id": "SECRET_TOKEN private chat"}, "invalid_request", id="id"),
        pytest.param({"generation": True}, "invalid_request", id="boolean_generation"),
        pytest.param(
            {"generation": 0.5}, "invalid_request", id="fractional_generation"
        ),
        pytest.param({"generation": 2**53}, "invalid_request", id="large_generation"),
        pytest.param({"generation": 6}, "stale_generation", id="old_generation"),
        pytest.param({"generation": 8}, "stale_generation", id="future_generation"),
        pytest.param({"method": ""}, "invalid_request", id="empty_method"),
        pytest.param(
            {"method": "return os.execute('private chat')"},
            "invalid_request",
            id="code_as_method",
        ),
        pytest.param({"arguments": []}, "invalid_request", id="array_arguments"),
        pytest.param({"arguments": None}, "invalid_request", id="null_arguments"),
        pytest.param(
            {"extra": "SECRET_TOKEN private chat"}, "invalid_request", id="extra_field"
        ),
    ],
)
def test_invalid_envelope_is_rejected_before_acceptance(
    send: Callable[..., list[dict]], changes: dict[str, object], error: str
) -> None:
    records = send(changes)
    assert len(records) == 1
    assert "accepted" not in records[0]
    assert records[0]["result"] == {"ok": False, "error": error}


@pytest.mark.parametrize(
    "payload",
    [
        pytest.param(b"null", id="null"),
        pytest.param(b"[]", id="array"),
        pytest.param(b"{}", id="empty_object"),
        pytest.param(b"SECRET_TOKEN private chat", id="plain_text"),
        pytest.param(b'{"v":1+2}', id="expression"),
        pytest.param(b"{'v':1}", id="single_quotes"),
        pytest.param(b'{"v":1,/* comment */"id":2}', id="comment"),
        pytest.param(b'{"v":1,"v":1}', id="duplicate_key"),
        pytest.param(b'{"v":null,"v":1}', id="duplicate_null_key"),
        pytest.param(b'{"v":1,}', id="trailing_comma"),
        pytest.param(b'{"v":01}', id="leading_zero"),
        pytest.param(b'{"v":+1}', id="leading_plus"),
        pytest.param(b'{"v":1.}', id="missing_fraction"),
        pytest.param(b'{"v":1e}', id="missing_exponent"),
        pytest.param(b'{"v":1e999}', id="infinity"),
        pytest.param(b'{"v":NaN}', id="nan"),
        pytest.param(rb'{"v":"\x41"}', id="hex_escape"),
        pytest.param(rb'{"v":"\ud800"}', id="unpaired_high_surrogate"),
        pytest.param(rb'{"v":"\udc00"}', id="unpaired_low_surrogate"),
        pytest.param(rb'{"v":"\ud800\u0000"}', id="invalid_surrogate_pair"),
        pytest.param(b'{"v":"\xff"}', id="invalid_utf8"),
        pytest.param(b'{"v":"\x00"}', id="nul"),
        pytest.param(b'{"v":true false}', id="adjacent_values"),
        pytest.param(b'{"v":1} trailing', id="trailing_text"),
        pytest.param(b"[" * 65 + b"0" + b"]" * 65, id="deep_nesting"),
    ],
)
def test_malformed_payload_returns_an_unidentified_error(
    send: Callable[..., list[dict]], payload: bytes
) -> None:
    records = send(payload=payload)
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
def test_accepted_command_failure_is_reported(
    send: Callable[..., list[dict]], method: str, error: str
) -> None:
    accepted, result = send({"method": method})
    assert accepted["accepted"] is True
    assert result["result"] == {"ok": False, "error": error}


def test_json_values_keep_their_types(send: Callable[..., list[dict]]) -> None:
    values = {
        "null": None,
        "empty": {},
        "array": [None, {}, [], False, 0, "", 0.25],
        "text": '你好👩🏽‍💻\x00\n\t"\\',
        "number": -0.25e3,
    }
    assert send({"method": "echo", "arguments": values})[-1]["result"] == {
        "ok": True,
        "data": values,
    }


def test_missing_result_is_null(send: Callable[..., list[dict]]) -> None:
    assert send({"method": "nothing"})[-1]["result"] == {"ok": True, "data": None}


@pytest.mark.parametrize("excess", [0, 1], ids=["at_limit", "over_limit"])
def test_response_byte_limit(send: Callable[..., list[dict]], excess: int) -> None:
    empty = HEADER | {"result": {"ok": True, "data": ""}}
    size = 65536 - len(b"DST_RPC|" + encode(empty) + b"\n")
    result = send({"method": "large", "arguments": {"size": size + excess}})
    assert result[-1]["result"] == (
        {"ok": False, "error": "response_too_large"}
        if excess
        else {"ok": True, "data": "x" * size}
    )


@pytest.mark.parametrize("excess", [0, 1], ids=["at_limit", "over_limit"])
def test_request_byte_limit(send: Callable[..., list[dict]], excess: int) -> None:
    size = 4096 - len(
        b"DST_RPC|" + encode(REQUEST | {"arguments": {"pad": ""}}) + b"\n"
    )
    result = send({"arguments": {"pad": "x" * (size + excess)}})
    assert result[-1]["result"] == (
        {"ok": False, "error": "invalid_request"}
        if excess
        else {"ok": True, "data": True}
    )


@pytest.mark.parametrize(
    ("scenario", "error"), [("not_ready", "not_ready"), ("write_error", "lua_error")]
)
def test_unavailable_driver_does_not_accept_calls(
    send: Callable[..., list[dict]], scenario: str, error: str
) -> None:
    records = send(scenario=scenario)
    assert len(records) == 1
    assert records[0]["result"] == {"ok": False, "error": error}
