import pytest
from pydantic import ValidationError

from dst_server.events import GAME_EVENT_ADAPTER, player, server, world

NONCE = "01ARZ3NDEKTSV4RRFFQ69G5FAV"


def test_game_event_schema_is_strict() -> None:
    data = {
        "v": 2,
        "generation": 1,
        "session_id": "SESSION",
        "nonce": NONCE,
        "seq": 1,
        "event": "dst.world.state_changed",
        "tick": 10,
        "monotonic_ms": 20,
        "cycle": 2,
        "data": {"name": "cycles", "value": 2},
    }

    event = GAME_EVENT_ADAPTER.validate_python(data, strict=True)

    assert isinstance(event, world.StateChangedEvent)
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
    for nonce in (
        "0123456789abcdef",
        NONCE.lower(),
        "8" + "0" * 25,
        "测" * 26,
    ):
        with pytest.raises(ValidationError, match="string_pattern_mismatch"):
            GAME_EVENT_ADAPTER.validate_python(data | {"nonce": nonce}, strict=True)


def test_coordinate_migration_without_portal_is_valid() -> None:
    event = GAME_EVENT_ADAPTER.validate_python(
        {
            "v": 2,
            "generation": 1,
            "session_id": "SESSION",
            "nonce": NONCE,
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


def test_combat_hit_without_resolved_damage_is_valid() -> None:
    event = GAME_EVENT_ADAPTER.validate_json(
        """{
            "v": 2,
            "generation": 1,
            "session_id": "SESSION",
            "nonce": "01ARZ3NDEKTSV4RRFFQ69G5FAV",
            "seq": 1,
            "event": "dst.player.combat_hit",
            "tick": 10,
            "monotonic_ms": 20,
            "cycle": 2,
            "data": {
                "player": {
                    "prefab": "wilson",
                    "guid": 42,
                    "userid": "KU_TEST",
                    "position": null
                },
                "damage": 0,
                "weapon": null,
                "stimuli": null,
                "special_damage": [],
                "caused_by_action_sequence": null,
                "target": {
                    "prefab": "hound",
                    "guid": 43,
                    "userid": null,
                    "position": null
                },
                "damage_resolved": null,
                "redirected": null
            }
        }""",
        strict=True,
    )

    assert isinstance(event, player.CombatHitEvent)
    assert event.data.damage_resolved is None


def telemetry_error(**changes: object) -> dict[str, object]:
    return {
        "v": 2,
        "nonce": NONCE,
        "generation": 1,
        "session_id": "SESSION",
        "seq": 1,
        "event": "dst.telemetry.error",
        "tick": 0,
        "monotonic_ms": 0,
        "cycle": None,
        "data": {
            "stage": "player.finishedwork",
            "message": "callback_failed",
            "count": 1,
        },
    } | changes


def test_telemetry_diagnostic_has_a_safe_strict_contract() -> None:
    event = GAME_EVENT_ADAPTER.validate_python(telemetry_error(), strict=True)

    assert event.v == 2
    assert event.generation == 1
    assert event.session_id == "SESSION"
    assert event.data.message == "callback_failed"


@pytest.mark.parametrize(
    "changes",
    [
        {"v": 1},
        {"generation": -1},
        {"generation": "1"},
        {"generation": True},
        {"session_id": ""},
        {"seq": 0},
        {"event": "dst.unknown"},
        {"data": {"stage": "callback", "message": "callback_failed", "count": 0}},
        {"data": {"stage": "callback", "message": "SECRET_TOKEN", "count": 1}},
        {
            "data": {
                "stage": "callback\nprivate chat",
                "message": "callback_failed",
                "count": 1,
            }
        },
        {
            "data": {
                "stage": "callback",
                "message": "callback_failed",
                "count": 1,
                "raw": "secret",
            }
        },
    ],
)
def test_telemetry_diagnostic_rejects_unsafe_or_ambiguous_fields(
    changes: dict[str, object],
) -> None:
    with pytest.raises(ValidationError):
        GAME_EVENT_ADAPTER.validate_python(telemetry_error(**changes), strict=True)


def test_fd5_server_events_are_typed_without_losing_unknown_lines() -> None:
    saved = server.parse_event("DST_Saved|session/TEST/26")

    assert isinstance(saved, server.SavedEvent)
    assert saved.snapshot == 26
    unknown = server.parse_event("DST_Stats|players=2")
    assert isinstance(unknown, server.UnknownEvent)
    assert unknown.line == "DST_Stats|players=2"


@pytest.mark.parametrize(
    "line",
    [
        "DST_Master_Ready|" + "x" * 4097,
        "DST_SessionId|" + "x" * 129,
        "DST_Saved|" + "x" * 4097,
        "DST_Saved|" + "9" * 4301,
    ],
    ids=("ready-detail", "session-id", "saved-path", "saved-snapshot"),
)
def test_invalid_fd5_event_fields_are_preserved_as_unknown(line: str) -> None:
    assert server.parse_event(line) == server.UnknownEvent(line=line)
