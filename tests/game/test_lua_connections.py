from pathlib import Path

import pytest

from dst_server.events import GAME_EVENT_ADAPTER
from dst_server.events.connection import (
    ClientAuthenticatedEvent,
    ClientDisconnectedEvent,
    PresenceEvent,
)
from dst_server.events.world import TelemetryErrorEvent
from tests.helpers import native_functions, run_lua_process


@pytest.fixture(scope="module")
def native_connections(tmp_path_factory: pytest.TempPathFactory) -> Path:
    names = {
        "GetPlayerClientTable",
        "ClientAuthenticationComplete",
        "ClientDisconnected",
    }
    path = tmp_path_factory.mktemp("native-connections") / "handlers.lua"
    path.write_text(
        native_functions("networking.lua", names)
        + "\nreturn {"
        + ",".join(f"{name}={name}" for name in sorted(names))
        + "}\n"
    )
    return path


@pytest.mark.parametrize(
    "scenario",
    [
        "critical",
        "history",
        "off",
        "optional_failure",
        "snapshot_failure",
        "invalid_authentication",
    ],
)
def test_native_connections_and_periodic_presence(
    native_connections: Path, lua_runtime: str, scenario: str
) -> None:
    root = Path(__file__).parents[2]
    output = run_lua_process(
        lua_runtime,
        root / "tests/lua/connections_spec.lua",
        root,
        native_connections,
        scenario,
    )
    records = [
        GAME_EVENT_ADAPTER.validate_json(line.removeprefix("DST_OTEL|"), strict=True)
        for line in output.decode().splitlines()
    ]
    for sequence, record in enumerate(records, 1):
        assert record.seq == sequence
        assert record.generation == 4
        assert record.session_id == "SESSION"
    events = [
        record for record in records if not isinstance(record, TelemetryErrorEvent)
    ]
    assert len(events) == 4
    initial, authenticated, disconnected, periodic = events
    assert isinstance(initial, PresenceEvent)
    assert initial.data.reason == "startup"
    assert initial.data.clients == ("KU_A",)
    assert [player.guid for player in initial.data.players] == [20, 21]
    assert {player.userid for player in initial.data.players} == {"KU_A"}
    assert isinstance(authenticated, ClientAuthenticatedEvent)
    assert isinstance(disconnected, ClientDisconnectedEvent)
    assert authenticated.data.userid == disconnected.data.userid == "KU_LOBBY"
    assert isinstance(periodic, PresenceEvent)
    assert periodic.data.reason == "periodic"
    assert periodic.data.clients == ("KU_A",)
    assert periodic.data.players == ()
    assert periodic.data.max_players == 9
    assert periodic.monotonic_ms - initial.monotonic_ms == 60_000
    assert periodic.data.health.generation == 4
    assert periodic.data.health.events_emitted == periodic.seq - 1
    expected_status = (
        "disabled"
        if scenario == "off"
        else "failed"
        if scenario == "optional_failure"
        else "degraded"
        if scenario in {"snapshot_failure", "invalid_authentication"}
        else "active"
    )
    assert periodic.data.health.telemetry_status == expected_status
    assert periodic.data.health.errors == (
        0 if expected_status in {"active", "disabled"} else 1
    )
