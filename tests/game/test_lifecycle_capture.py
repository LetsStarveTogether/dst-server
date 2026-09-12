from pathlib import Path

import orjson
import pytest
from pydantic import ValidationError

from dst_server.events.player import CombatData, RevivedEvent
from dst_server.events.world import PauseChangedEvent, StateChangedEvent
from dst_server.runtime.operational import classify_log
from tests.lua.helpers import native_functions, native_scripts, run_lua_process


@pytest.fixture(scope="module")
def pause_handlers(tmp_path_factory: pytest.TempPathFactory) -> Path:
    path = tmp_path_factory.mktemp("pause-handlers") / "handlers.lua"
    path.write_text(
        "local Paused, Autopaused, GameAutopaused = false, false, false\n"
        + native_functions(
            "mainfunctions.lua", {"OnServerPauseDirty", "OnSimPaused", "OnSimUnpaused"}
        )
    )
    return path


@pytest.mark.parametrize("profile", ["critical", "history", "off"])
def test_native_revival_and_pause_facts(
    luajit: str, pause_handlers: Path, profile: str
) -> None:
    root = Path(__file__).parents[2]
    output = run_lua_process(
        luajit,
        root / "tests/lua/lifecycle_contract.lua",
        root,
        native_scripts(),
        pause_handlers,
        profile,
    )
    lines = output.decode().splitlines()
    if profile == "off":
        assert lines == []
        return
    records = [
        orjson.loads(line.removeprefix("DST_OTEL|"))
        for line in lines
        if line.startswith("DST_OTEL|")
    ]
    revivals = [
        RevivedEvent.model_validate(record, strict=True) for record in records[:3]
    ]
    assert [record.data.method for record in revivals] == ["ghost", "corpse", "charlie"]
    assert [record.data.corpse for record in revivals] == [False, True, False]
    assert revivals[1].data.reviver == revivals[1].data.player
    assert revivals[2].data.reviver is None
    pauses = [
        PauseChangedEvent.model_validate(record, strict=True).data.model_dump()
        for record in records[3:9]
    ]
    assert pauses == [
        {
            "domain": "server",
            "pause": True,
            "autopause": False,
            "gameautopause": False,
            "source": "玩家",
        },
        {
            "domain": "server",
            "pause": False,
            "autopause": True,
            "gameautopause": False,
            "source": None,
        },
        {
            "domain": "server",
            "pause": False,
            "autopause": False,
            "gameautopause": True,
            "source": None,
        },
        {
            "domain": "server",
            "pause": False,
            "autopause": False,
            "gameautopause": False,
            "source": None,
        },
        {"domain": "simulation", "paused": True},
        {"domain": "simulation", "paused": False},
    ]
    assert records[9]["event"] == "dst.telemetry.error"
    assert records[9]["data"]["message"] == "callback_failed"
    combat = [
        CombatData.model_validate_json(line.removeprefix("COMBAT|"), strict=True)
        for line in lines
        if line.startswith("COMBAT|")
    ]
    assert [data.from_doattack for data in combat] == [None, False, True]


@pytest.mark.parametrize(
    "reason",
    [
        "ID_DST_NO_FREE_PLAYER_SLOTS",
        "ID_DST_GAME_SESSION_AUTH_FAILED",
        "ID_DST_SHARD_SILENT_DISCONNECT",
        "MOD_CONNECTION_REASON",
    ],
)
def test_native_connection_closed_reason(reason: str) -> None:
    assert classify_log(f"CloseConnectionWithReason: {reason}") == (
        "dst.connection.closed",
        {"reason": reason},
        "INFO",
    )


@pytest.mark.parametrize(
    "line",
    [
        "[Say] (KU_PLAYER) Name: CloseConnectionWithReason: ID_DST_USER_BANNED",
        "CloseConnectionWithReason: ID_DST_USER_KICKED token=private",
        "CloseConnectionWithReason: " + "A" * 129,
        "CloseConnectionWithReason: lowercase",
        "CloseConnectionWithReason: ",
        "Server Paused",
        "Server Autopaused",
        "Server Unpaused",
        "Sim paused",
        "Sim unpaused",
    ],
)
def test_native_pause_text_does_not_duplicate_events_or_parse_chat(line: str) -> None:
    assert classify_log(line) is None


@pytest.mark.parametrize(
    "name",
    ["season", "phase", "cavephase", "moonphase", "cavemoonphase", "nightmarephase"],
)
def test_mod_world_state_values_remain_open(name: str) -> None:
    event = {
        "v": 2,
        "nonce": "01ARZ3NDEKTSV4RRFFQ69G5FAV",
        "generation": 1,
        "session_id": "SESSION",
        "seq": 1,
        "event": "dst.world.state_changed",
        "tick": 10,
        "monotonic_ms": 20,
        "cycle": 3,
        "data": {"name": name, "value": "mod_custom_phase"},
    }
    assert StateChangedEvent.model_validate(event).data.value == "mod_custom_phase"
    for value in ("", "x" * 129, 4):
        event["data"] = {"name": name, "value": value}
        with pytest.raises(ValidationError):
            StateChangedEvent.model_validate(event)
