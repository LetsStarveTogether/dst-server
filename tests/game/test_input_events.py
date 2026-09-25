from pathlib import Path

import pytest

from dst_server.events import GAME_EVENT_ADAPTER
from dst_server.events.player import (
    AppearanceRequestedEvent,
    EmoteRequestedEvent,
    RescueRequestedEvent,
)
from dst_server.events.world import TelemetryErrorEvent
from tests.lua.helpers import run_lua_process


@pytest.mark.parametrize("profile", ["critical", "history", "off"])
def test_native_player_requests(
    native_scripts: Path, lua_runtime: str, profile: str
) -> None:
    root = Path(__file__).parents[2]
    output = run_lua_process(
        lua_runtime,
        root / "tests/lua/input_events_spec.lua",
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
    emotes = [event.data for event in events if isinstance(event, EmoteRequestedEvent)]
    assert [data.emote for data in emotes] == ["wave", "toast", "chicken", "wave"] + [
        "dance"
    ] * 10
    assert all(data.userid == "KU_PLAYER" for data in emotes)
    assert emotes[0].player is not None
    assert emotes[0].player.prefab == "wilson"
    assert emotes[3].player is None
    rescues = [event for event in events if isinstance(event, RescueRequestedEvent)]
    assert len(rescues) == 4
    assert all(event.data.userid == "KU_PLAYER" for event in rescues)
    appearances = [
        event.data for event in events if isinstance(event, AppearanceRequestedEvent)
    ]
    assert len(appearances) == 2
    appearance = appearances[0]
    assert appearance.userid == "KU_PLAYER"
    assert appearance.requested.model_dump() == {
        "prefab": "wilson",
        "skin_base": "wilson_rose",
        "clothing_body": "body_unowned",
        "clothing_hand": "hand_owned",
        "clothing_legs": None,
        "clothing_feet": None,
    }
    assert appearance.validated == appearance.requested.replace(clothing_body=None)
    assert appearances[1].requested.prefab == "wonkey"
    assert appearances[1].validated.prefab == "wilson"
    assert appearances[1].validated.skin_base is None
    assert [
        event.data.stage for event in events if isinstance(event, TelemetryErrorEvent)
    ] == ["player.rescue_requested", "player.appearance_requested"]
