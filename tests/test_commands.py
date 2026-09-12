import math

import pytest
from pydantic import ValidationError
from ulid import ULID

from dst_server import commands
from dst_server.models.cluster import ObservationCursor
from dst_server.timeouts import DEFAULT_COMMAND_TIMEOUT, DEFAULT_RELOAD_TIMEOUT


def test_requests_preserve_typed_arguments_and_operation_policy() -> None:
    request = commands.Pause(paused=False)
    assert request.method == "pause"
    assert request.arguments == {"paused": False}
    assert request.timeout == DEFAULT_COMMAND_TIMEOUT
    assert commands.operation("shard", request).mutation
    assert not commands.operation("shard", commands.Status()).mutation
    assert commands.Reset().timeout == DEFAULT_RELOAD_TIMEOUT


@pytest.mark.parametrize(
    ("command", "accepted", "rejected"),
    [
        (commands.Status(), ("shard", "agent"), ("cluster",)),
        (commands.ClusterStatusQuery(), ("cluster",), ("shard", "agent")),
        (commands.Reset(), ("cluster", "agent"), ("shard",)),
        (commands.Activate(), ("agent",), ("cluster", "shard")),
        (commands.ReadConfiguration(), ("cluster",), ("shard", "agent")),
        (commands.IsWhitelisted(userid="KU_PLAYER"), ("cluster", "agent"), ("shard",)),
    ],
)
def test_capability_scope_is_an_explicit_allowlist(
    command: commands.Request[object],
    accepted: tuple[str, ...],
    rejected: tuple[str, ...],
) -> None:
    for scope in accepted:
        assert commands.operation(scope, command).request is type(command)
    for scope in rejected:
        with pytest.raises(ValueError, match="not available"):
            commands.operation(scope, command)


@pytest.mark.parametrize("value", [True, 0, -1, math.nan, math.inf, "1"])
def test_all_requests_reject_invalid_deadlines(value: object) -> None:
    with pytest.raises(ValidationError):
        commands.Status.model_validate({"timeout": value})


@pytest.mark.parametrize(
    ("command_type", "arguments"),
    [
        (commands.Pause, {"paused": 1}),
        (commands.Give, {"userid": "KU_PLAYER", "item": "twigs", "count": 65}),
        (commands.Give, {"userid": "KU_PLAYER", "item": "twigs", "count": True}),
        (commands.Rollback, {"count": -1}),
        (commands.Snapshots, {"limit": 101}),
        (commands.Snapshots, {"before": -1}),
        (commands.SetVitals, {"userid": "KU_PLAYER"}),
        (commands.SetVitals, {"userid": "KU_PLAYER", "health": 1.01}),
        (commands.Teleport, {"userid": "KU_PLAYER", "x": math.nan, "y": 0, "z": 0}),
    ],
)
def test_invalid_commands_fail_before_dispatch(
    command_type: type[commands.Request[object]], arguments: dict[str, object]
) -> None:
    with pytest.raises(ValidationError):
        command_type.model_validate(arguments)


def test_wire_round_trip_preserves_cursor_identity_and_presence() -> None:
    cursor = ObservationCursor(attempt=ULID(), sequence=0)
    request = commands.WaitSaved(cursor=cursor, snapshot=None, timeout=1.5)
    restored = commands.parse_request(commands.encode_request(request), scope="agent")
    assert isinstance(restored, commands.WaitSaved)
    assert restored == request
    assert restored.cursor == cursor
    assert restored.arguments == {"cursor": cursor, "snapshot": None}


@pytest.mark.parametrize(
    "payload",
    [
        b'{"method":"__getattribute__","arguments":{}}',
        b'{"method":"reset","arguments":{}}',
        b'{"method":"status","arguments":{"extra":true}}',
        b'{"method":"status","arguments":{},"extra":true}',
        b'{"method":"status","method":"kill","arguments":{}}',
    ],
)
def test_wire_request_cannot_bypass_scope_or_parameter_validation(
    payload: bytes,
) -> None:
    with pytest.raises(ValueError, match=r".+"):
        commands.parse_request(payload, scope="shard")


@pytest.mark.parametrize("timeout", [0, -1, float("nan"), float("inf"), True])
def test_copied_requests_are_revalidated(timeout: object) -> None:
    copied = commands.Save().model_copy(update={"timeout": timeout})
    with pytest.raises(ValidationError):
        commands.operation("shard", copied)
    with pytest.raises(ValidationError):
        commands.encode_request(copied)


@pytest.mark.parametrize(
    "payload",
    [
        b'{"key":1,"key":2}',
        rb'{"key":1,"\u006bey":2}',
        rb'{"nested":[{"a\\\"}":1,"a\\\"}":2}]}',
        b'{"":0,"":1}',
    ],
)
def test_json_structure_rejects_duplicate_keys(payload: bytes) -> None:
    with pytest.raises(ValueError, match="duplicate JSON object key"):
        commands.validate_json_structure(payload)


@pytest.mark.parametrize(
    "payload",
    [
        b'[{"key":1},{"key":2}]',
        b'{"key":{"key":1},"other":{"key":2}}',
        rb'{"text":"{\"key\":1,\"key\":2}","literal":"NaN"}',
        rb'{"\u006bey":1,"a\\\"}":2}',
        b"[0,true,false,null,18446744073709551616]",
    ],
)
def test_json_structure_accepts_distinct_keys_and_string_contents(
    payload: bytes,
) -> None:
    commands.validate_json_structure(payload)


@pytest.mark.parametrize("constant", [b"NaN", b"Infinity", b"-Infinity"])
@pytest.mark.parametrize("nested", [False, True])
def test_json_structure_rejects_nonfinite_constants(
    constant: bytes, nested: bool
) -> None:
    payload = b'{"value":' + constant + b"}" if nested else constant
    with pytest.raises(ValueError, match="invalid JSON constant"):
        commands.validate_json_structure(payload)
