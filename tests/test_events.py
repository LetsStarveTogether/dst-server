from __future__ import annotations

import pytest
from pydantic import ValidationError

from dst_server import ServerSavedEvent, UnknownServerEvent
from dst_server.events import (
    GAME_EVENT_ADAPTER,
    WorldStateChangedEvent,
    parse_server_event,
)


def test_game_event_schema_is_strict() -> None:
    data = {
        "v": 1,
        "nonce": "0123456789abcdef",
        "seq": 1,
        "event": "dst.world.state_changed",
        "tick": 10,
        "monotonic_ms": 20,
        "cycle": 2,
        "data": {"name": "cycles", "value": 2},
    }

    event = GAME_EVENT_ADAPTER.validate_python(data, strict=True)

    assert isinstance(event, WorldStateChangedEvent)
    assert event.data.value == 2
    with pytest.raises(ValidationError, match="extra_forbidden"):
        GAME_EVENT_ADAPTER.validate_python(data | {"unexpected": True}, strict=True)
    with pytest.raises(ValidationError, match="int_type"):
        GAME_EVENT_ADAPTER.validate_python(data | {"tick": "10"}, strict=True)
    with pytest.raises(ValidationError, match="literal_error"):
        GAME_EVENT_ADAPTER.validate_python(
            data | {"data": {"name": "season", "value": "day"}},
            strict=True,
        )


def test_coordinate_migration_without_portal_is_valid() -> None:
    event = GAME_EVENT_ADAPTER.validate_python(
        {
            "v": 1,
            "nonce": "0123456789abcdef",
            "seq": 1,
            "event": "dst.player.migration_started",
            "tick": 10,
            "monotonic_ms": 20,
            "cycle": 2,
            "data": {
                "player": {
                    "prefab": "wilson",
                    "guid": 42,
                    "userid": "KU_TEST",
                    "position": {"x": 1.0, "y": 0.0, "z": 2.0},
                },
                "destination_shard_id": "2",
                "portal_id": None,
                "destination": {"x": 3.0, "y": 0.0, "z": 4.0},
            },
        },
        strict=True,
    )

    assert event.data.portal_id is None
    assert event.data.destination is not None
    assert event.data.destination.z == 4


def test_fd5_server_events_are_typed_without_losing_unknown_lines() -> None:
    saved = parse_server_event("DST_Saved|session/TEST/26")

    assert isinstance(saved, ServerSavedEvent)
    assert saved.snapshot == 26
    unknown = parse_server_event("DST_Stats|players=2")
    assert isinstance(unknown, UnknownServerEvent)
    assert unknown.line == "DST_Stats|players=2"
