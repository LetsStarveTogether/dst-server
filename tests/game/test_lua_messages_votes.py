from pathlib import Path

import pytest

from dst_server.events import GAME_EVENT_ADAPTER
from dst_server.events.messages import (
    AnnouncementEvent,
    DiceRolledEvent,
    SkinReceivedEvent,
)
from dst_server.events.vote import (
    VoteCastEvent,
    VoteClosedEvent,
    VoteResultEvent,
    VoteStartedEvent,
)
from dst_server.events.world import TelemetryErrorEvent
from tests.lua.helpers import run_lua_process


@pytest.mark.parametrize("profile", ["critical", "history", "off"])
def test_message_wrappers(native_scripts: Path, lua_runtime: str, profile: str) -> None:
    root = Path(__file__).parents[2]
    output = run_lua_process(
        lua_runtime,
        root / "tests/lua/messages_spec.lua",
        root,
        native_scripts,
        profile,
    )
    events = [
        GAME_EVENT_ADAPTER.validate_json(line, strict=True)
        for line in output.splitlines()
    ]
    if profile == "off":
        assert events == []
        return
    assert [event.event for event in events] == [
        "dst.player.chat",
        "dst.player.chat",
        "dst.player.chat",
        "dst.server.announcement",
        "dst.server.announcement",
        "dst.server.announcement",
        "dst.player.skin_received",
        "dst.server.system_message",
        "dst.player.dice_rolled",
        "dst.telemetry.error",
    ]
    assert [
        event.data.kind for event in events if isinstance(event, AnnouncementEvent)
    ] == ["default", "default", "mod_custom_湿季"]
    skin = next(event for event in events if isinstance(event, SkinReceivedEvent))
    assert skin.data.name == "玩家"
    assert skin.data.skin == "wilson_rose"
    dice = next(event for event in events if isinstance(event, DiceRolledEvent))
    assert dice.data.model_dump(mode="json") == {
        "userid": "KU_A",
        "name": "玩家",
        "prefab": "wilson",
        "rolls": [1, 4, 6],
        "max": 6,
    }
    assert not any(isinstance(event, VoteResultEvent) for event in events)


@pytest.mark.parametrize(
    "scenario",
    [
        "passed",
        "failed",
        "cancelled",
        "custom",
        "secondary",
        "error",
        "capture_error",
    ],
)
def test_vote_event_capture(
    native_scripts: Path, lua_runtime: str, scenario: str
) -> None:
    root = Path(__file__).parents[2]
    output = run_lua_process(
        lua_runtime,
        root / "tests/lua/votes_spec.lua",
        root,
        native_scripts,
        scenario,
    )
    events = [
        GAME_EVENT_ADAPTER.validate_json(line, strict=True)
        for line in output.splitlines()
    ]
    if scenario == "secondary":
        assert events == []
        return
    assert isinstance(events[0], VoteStartedEvent)
    assert events[0].data.vote_id == "01ARZ3NDEKTSV4RRFFQ69G5FAV:4:1"
    assert events[0].data.command == ("mod_test" if scenario == "custom" else "kick")
    assert len(events[0].data.options) == (3 if scenario == "custom" else 2)
    assert events[0].data.target_userid == "KU_TARGET"
    assert [
        event.data.userid for event in events if isinstance(event, VoteCastEvent)
    ] == ["KU_A", "KU_B"]
    assert sum(isinstance(event, VoteClosedEvent) for event in events) == 1
    results = [event for event in events if isinstance(event, VoteResultEvent)]
    if scenario in {"cancelled", "error", "capture_error"}:
        assert results == []
    else:
        assert len(results) == 1
        result = results[0].data
        assert result.passed == (scenario in {"passed", "custom"})
        assert result.total == 3
        assert result.total_voted == 2
        assert result.total_not_voted == 0
        assert result.options == ((0, 0, 2) if scenario == "custom" else (2, 0))
        expected_selection = (
            (3 if scenario == "custom" else 1) if result.passed else None
        )
        assert result.selection == expected_selection
    diagnostics = [event for event in events if isinstance(event, TelemetryErrorEvent)]
    assert len(diagnostics) == (1 if scenario == "capture_error" else 0)
    if diagnostics:
        assert diagnostics[0].data.stage == "vote.result"
        assert diagnostics[0].data.message == "callback_failed"
    assert all(
        isinstance(
            event, (VoteStartedEvent, VoteCastEvent, VoteClosedEvent, VoteResultEvent)
        )
        and event.data.vote_id == events[0].data.vote_id
        for event in events
        if not isinstance(event, TelemetryErrorEvent)
    )
