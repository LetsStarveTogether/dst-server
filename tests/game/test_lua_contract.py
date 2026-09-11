import subprocess  # ruff:ignore[suspicious-subprocess-import]
from pathlib import Path
from typing import Any

import pytest

from dst_server import commands as c
from dst_server.events import GAME_EVENT_ADAPTER
from dst_server.game.client import _METHODS
from dst_server.game.rpc import BOOL_RESPONSE, response_adapter

PREFIX = "DST_OTEL|"

RPC_ADAPTERS = {
    method: response_adapter(c.operation("agent", command.method).result_type or bool)
    for command, method in _METHODS.items()
} | {"save": BOOL_RESPONSE}


def run_lua_contract(script: str, luajit: str) -> list[str]:
    root = Path(__file__).parents[2]
    result = subprocess.run(  # ruff:ignore[subprocess-without-shell-equals-true]
        [
            luajit,
            str(root / f"tests/lua/{script}"),
            str(root / "src/dst_server/lua"),
            str(root / "tests/lua"),
            str(root / "dst-scripts/scripts"),
        ],
        capture_output=True,
        text=True,
        timeout=5,
        check=False,
    )

    assert result.returncode == 0, result.stderr or result.stdout
    return result.stdout.splitlines()


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
    assert data["get_room"]["name"] == "Test Room"
    assert data["get_room"]["player_count"] == 2
    assert data["get_room"]["max_players"] == 6
    assert data["get_room"]["playstyle"] is None
    assert data["get_world"]["age"] == 1000
    assert data["get_world"]["day"] == 11
    assert data["get_world"]["phase"] == "day"
    assert data["get_world"]["season"] == "autumn"
    assert data["get_runtime"]["session_id"] == "SESSION"
    assert data["get_runtime"]["snapshot"] == 26
    assert data["get_snapshots"] == {
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
    assert data["get_mods"] == [
        {"id": "workshop-1", "name": "Test Mod", "version": "1.2.3"}
    ]
    assert data["get_shards"] == [
        {"id": "1", "name": "Master", "is_current": True, "ready": True, "tags": []},
        {
            "id": "2",
            "name": "Caves",
            "is_current": False,
            "ready": True,
            "tags": ["cave"],
        },
    ]


def test_lua_player_values_and_loading_state(rpc_data: dict[str, Any]) -> None:
    data = rpc_data
    player, loading = data["get_players"]
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
    inventory = rpc_data["get_player_inventory"]
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
    assert data["get_blocklist"] == ["KU_BLOCKED", "KU_KEEP", "Steam_ONLY"]
    for method, adapter in RPC_ADAPTERS.items():
        if adapter is BOOL_RESPONSE:
            assert data[method] is True, method
    assert data["give_item"] == 1
    assert data["remove_item"] == 1
    assert data["execute_script"] == {"answer": 42}
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
