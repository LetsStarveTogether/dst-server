"""Lua RPC framing and results at the native console boundary."""

import json
from collections.abc import Callable
from pathlib import Path

import pytest

NONCE = "01ARZ3NDEKTSV4RRFFQ69G5FAV"
IDENTIFIER = "01ARZ3NDEKTSV4RRFFQ69G5FAW"
HEADER = {"nonce": NONCE, "id": IDENTIFIER, "generation": 7}
REQUEST = HEADER | {"method": "ping", "arguments": {}}
MAX_REPLY = 1024 * 1024


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
            assert len(line) <= MAX_REPLY
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
    ],
)
def test_unusable_envelope(send: Callable[..., list[dict]], payload: bytes) -> None:
    assert send(payload=payload) == [
        HEADER | {"id": None, "result": {"ok": False, "error": "invalid_request"}}
    ]


@pytest.mark.parametrize(
    "arguments",
    [
        pytest.param(b'{"n":1+2}', id="expression"),
        pytest.param(b"{'n':1}", id="single_quotes"),
        pytest.param(b'{"x":1,/* comment */"y":2}', id="comment"),
        pytest.param(b'{"n":1,"n":1}', id="duplicate_key"),
        pytest.param(b'{"n":null,"n":1}', id="duplicate_null_key"),
        pytest.param(b'{"n":1,}', id="trailing_comma"),
        pytest.param(b'{"n":01}', id="leading_zero"),
        pytest.param(b'{"n":+1}', id="leading_plus"),
        pytest.param(b'{"n":1.}', id="missing_fraction"),
        pytest.param(b'{"n":1e}', id="missing_exponent"),
        pytest.param(b'{"n":1e999}', id="infinity"),
        pytest.param(b'{"n":NaN}', id="nan"),
        pytest.param(rb'{"text":"\x41"}', id="hex_escape"),
        pytest.param(rb'{"text":"\ud800"}', id="unpaired_high_surrogate"),
        pytest.param(rb'{"text":"\udc00"}', id="unpaired_low_surrogate"),
        pytest.param(rb'{"text":"\ud800\u0000"}', id="invalid_surrogate_pair"),
        pytest.param(b'{"text":"\xff"}', id="invalid_utf8"),
        pytest.param(b'{"text":"\x00"}', id="nul"),
        pytest.param(b'{"n":true false}', id="adjacent_values"),
        pytest.param(b'{"n":1} trailing', id="trailing_text"),
        pytest.param(b'{"n":' + b"[" * 65 + b"0" + b"]" * 65 + b"}", id="deep_nesting"),
    ],
)
def test_malformed_arguments_discard_the_request_identity(
    send: Callable[..., list[dict]], arguments: bytes
) -> None:
    header = encode(HEADER | {"method": "ping"})
    payload = header[:-1] + b',"arguments":' + arguments + b"}"
    assert send(payload=payload) == [
        HEADER | {"id": None, "result": {"ok": False, "error": "invalid_request"}}
    ]


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
        "ascii": ['"', "\\", "\x00", "\x1f"],
        "number": -0.25e3,
    }
    assert send({"method": "echo", "arguments": values})[-1]["result"] == {
        "ok": True,
        "data": values,
    }


def test_missing_result_is_null(send: Callable[..., list[dict]]) -> None:
    assert send({"method": "nothing"})[-1]["result"] == {"ok": True, "data": None}


@pytest.mark.parametrize(
    "line_bytes", [65535, 65536, 65537, MAX_REPLY - 1, MAX_REPLY, MAX_REPLY + 1]
)
def test_response_byte_limit(send: Callable[..., list[dict]], line_bytes: int) -> None:
    empty = HEADER | {"result": {"ok": True, "data": ""}}
    size = line_bytes - len(b"DST_RPC|" + encode(empty) + b"\n")
    result = send({"method": "large", "arguments": {"size": size}})
    assert result[-1]["result"] == (
        {"ok": False, "error": "response_too_large"}
        if line_bytes > MAX_REPLY
        else {"ok": True, "data": "x" * size}
    )


@pytest.mark.parametrize(
    ("scenario", "followers", "skills"), [("ordinary", 1, 3), ("rich", 16, 15)]
)
def test_roster_and_player_detail(
    send: Callable[..., list[dict]], scenario: str, followers: int, skills: int
) -> None:
    accepted, completed = send({"method": "list_players"}, scenario=scenario)
    assert accepted == HEADER | {"accepted": True}
    roster = completed["result"]["data"]
    assert len(roster) == 64
    for index, player in enumerate(roster, 1):
        assert player["userid"] == f"KU_client{index:04d}"
        assert player["name"] == f"玩家 Wilson {index:02d}"
        assert player["age"]["days"] == 61
        assert player["position"]["x"] == pytest.approx(index + 1 / 3)
        assert "vitals" not in player
        assert "state" not in player
    _, completed = send(
        {"method": "get_player", "arguments": {"userid": roster[-1]["userid"]}},
        scenario=scenario,
    )
    detail = completed["result"]["data"]
    assert detail.pop("vitals")["health"]["current"] == pytest.approx(113.27)
    state = detail.pop("state")
    assert state["follower_count"] == len(state["followers"]) == followers
    assert len(state["activated_skills"]) == skills
    assert detail == roster[-1]


def test_avatar_loading_preserves_nulls(send: Callable[..., list[dict]]) -> None:
    _, listed = send({"method": "list_players"}, scenario="loading")
    player = listed["result"]["data"][0]
    assert player["prefab"] == "wilson"
    assert player["is_ghost"] is player["position"] is player["age"] is None
    _, queried = send(
        {"method": "get_player", "arguments": {"userid": player["userid"]}},
        scenario="loading",
    )
    assert queried["result"]["data"] == player | {"vitals": None, "state": None}


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
