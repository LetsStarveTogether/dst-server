from pathlib import Path
from typing import Any

import pytest

from dst_server import commands as c
from dst_server.events import GAME_EVENT_ADAPTER
from dst_server.game.rpc import SAVE_RESPONSE, response_adapter
from tests.lua.helpers import native_scripts, run_lua_process

PREFIX = "DST_OTEL|"

RPC_ADAPTERS = {
    spec.request.method: response_adapter(
        bool if spec.result_type is None else spec.result_type
    )
    for spec in c.OPERATIONS
    if spec.game
} | {"save": SAVE_RESPONSE}


def run_lua_contract(script: str, luajit: str) -> list[str]:
    root = Path(__file__).parents[2]
    output = run_lua_process(
        luajit,
        root / f"tests/lua/{script}",
        root / "src/dst_server/lua",
        root / "tests/lua",
        native_scripts(),
    )
    return output.decode().splitlines()


def test_all_real_lua_event_producers_match_python_contract(luajit: str) -> None:
    lines = run_lua_contract("event_contract.lua", luajit)
    assert all(line.startswith(PREFIX) for line in lines)

    events = [
        GAME_EVENT_ADAPTER.validate_json(
            line.removeprefix(PREFIX),
            strict=True,
        )
        for line in lines
    ]
    definitions = GAME_EVENT_ADAPTER.json_schema()["$defs"].values()
    schema_events = {
        definition["properties"]["event"]["const"]
        for definition in definitions
        if "event" in definition.get("properties", {})
    }
    assert {event.event for event in events} == schema_events - {"dst.telemetry.error"}
    assert [event.seq for event in events] == list(range(1, len(events) + 1))
    assert all(event.nonce == "01ARZ3NDEKTSV4RRFFQ69G5FAV" for event in events)
    assert all(event.v == 2 and event.generation == 1 for event in events)
    assert all(event.session_id == "SESSION" for event in events)


def test_native_announcement_repetition(luajit: str) -> None:
    assert run_lua_contract("announcement_contract.lua", luajit) == []


@pytest.fixture(scope="module")
def rpc_data(luajit: str) -> dict[str, Any]:
    lines = run_lua_contract("rpc_contract.lua", luajit)
    responses = dict(line.split("|", 1) for line in lines)

    assert len(responses) == len(lines), "duplicate method responses"
    assert responses.keys() == RPC_ADAPTERS.keys()
    data = {}
    for method, adapter in RPC_ADAPTERS.items():
        response = adapter.validate_json(responses[method], strict=True)
        assert response.ok, method
        data[method] = response.model_dump(mode="json")["data"]

    return data


def test_registered_lua_queries(rpc_data: dict[str, Any]) -> None:
    data = rpc_data
    assert data["health"] == {
        "protocol": 2,
        "generation": 1,
        "telemetry_status": "disabled",
        "last_error": None,
        "events_emitted": 0,
        "errors": 0,
    }
    assert data["room"]["name"] == "Test Room"
    assert data["room"]["player_count"] == 2
    assert data["room"]["max_players"] == 6
    assert data["room"]["playstyle"] is None
    assert data["world"]["age"] == 1000
    assert data["world"]["day"] == 11
    assert data["world"]["phase"] == "day"
    assert data["world"]["season"] == "autumn"
    assert data["runtime"]["session_id"] == "SESSION"
    assert data["runtime"]["snapshot"] == 26
    assert data["list_snapshots"] == {
        "session_id": "SESSION",
        "snapshots": [
            {
                "snapshot_id": 25,
                "world_file": "save/session/SESSION/0000000025",
                "metadata": None,
            }
        ],
        "has_more": True,
    }
    assert data["mods"] == [
        {"id": "workshop-1", "name": "Test Mod", "version": "1.2.3"}
    ]
    assert data["connected_shards"] == [
        {"id": "1", "name": "Master", "is_current": True, "ready": True, "tags": []},
        {
            "id": "2",
            "name": "Caves",
            "is_current": False,
            "ready": True,
            "tags": ["cave"],
        },
    ]


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("season", "wet"),
        ("season", "monsoon"),
        ("phase", "custom_mod_phase"),
        ("moon_phase", "eclipse"),
        ("precipitation", "ash"),
    ],
)
def test_world_response_preserves_mod_state(
    rpc_data: dict[str, Any], field: str, value: str
) -> None:
    response = RPC_ADAPTERS["world"].validate_python({
        "ok": True,
        "data": rpc_data["world"] | {field: value},
    })

    assert response.ok
    assert getattr(response.data, field) == value


def test_lua_player_values_and_loading_state(rpc_data: dict[str, Any]) -> None:
    data = rpc_data
    player, loading = data["list_players"]
    assert player["userid"] == "KU_TEST"
    assert player["position"] == {"x": 1, "y": 0, "z": 2}
    assert player["age"] == {"seconds": 100, "days": 2, "display_days": 3}
    assert player["vitals"] == {
        "health": {
            "current": 80,
            "maximum": 100,
            "percent": 0.8,
            "is_dead": False,
            "is_invincible": False,
        },
        "hunger": {"current": 50, "maximum": 100, "percent": 0.5},
        "sanity": {"current": 60, "maximum": 100, "percent": 0.6},
        "temperature": {"current": 25, "maximum": 70},
        "moisture": {"current": 10, "maximum": 100, "percent": 0.1},
    }
    assert player["state"]["skill_xp"] == 10
    assert player["state"]["available_skill_points"] == 2
    assert player["state"]["activated_skills"] == ["wilson_torch_1"]
    assert loading == data["get_player"]
    assert loading["userid"] == "KU_LOADING"
    assert loading["prefab"] is None
    assert loading["age"] is None
    assert loading["vitals"] is None


def test_lua_inventory_values(rpc_data: dict[str, Any]) -> None:
    inventory = rpc_data["inventory"]
    assert inventory["userid"] == "KU_TEST"
    assert inventory["items"] == [
        {
            "slot": 1,
            "item": {
                "prefab": "twigs",
                "guid": 100,
                "skin": "classic",
                "stack_size": 3,
                "moisture_percent": 0.1,
                "uses_percent": 0.8,
                "freshness_percent": 0.7,
                "fuel_percent": 0.6,
                "armor_percent": 0.5,
                "charge_percent": 0.4,
            },
        }
    ]
    assert inventory["equipment"][0]["slot"] == "hands"
    assert inventory["equipment"][0]["item"]["prefab"] == "axe"
    assert inventory["active_item"]["prefab"] == "torch"
    assert inventory["overflow"]["prefab"] == "backpack"
    assert inventory["overflow"]["slots"][0]["slot"] == 2
    assert inventory["overflow"]["slots"][0]["item"]["prefab"] == "rocks"


def test_lua_mutation_results(rpc_data: dict[str, Any]) -> None:
    data = rpc_data
    assert data["blocklist"] == ["KU_BLOCKED", "KU_KEEP", "Steam_ONLY"]
    for method, adapter in RPC_ADAPTERS.items():
        if adapter is response_adapter(bool):
            assert data[method] is True, method
    assert data["give"] == 1
    assert data["remove"] == 1
    assert data["execute_json"] == {"answer": 42}
    assert data["evaluate"] == {
        "output": "",
        "values": [
            {"type": "number", "text": "3"},
            {"type": "string", "text": "中文"},
            {"type": "nil", "text": "nil"},
        ],
        "error": None,
        "truncated": False,
    }
