import subprocess  # ruff:ignore[suspicious-subprocess-import]
from pathlib import Path

from dst_server.events import GAME_EVENT_ADAPTER
from dst_server.game.rpc import (
    BOOL_RESPONSE,
    DRIVER_RESPONSE,
    INT_RESPONSE,
    INVENTORY_RESPONSE,
    JSON_RESPONSE,
    MODS_RESPONSE,
    PLAYER_IDS_RESPONSE,
    PLAYER_RESPONSE,
    PLAYERS_RESPONSE,
    ROOM_RESPONSE,
    RUNTIME_RESPONSE,
    SHARDS_RESPONSE,
    WORLD_RESPONSE,
    ResponseAdapter,
)

PREFIX = "DST_OTEL|"

RPC_ADAPTERS: dict[str, ResponseAdapter[object]] = {
    "health": DRIVER_RESPONSE,
    "get_room": ROOM_RESPONSE,
    "get_world": WORLD_RESPONSE,
    "get_runtime": RUNTIME_RESPONSE,
    "get_mods": MODS_RESPONSE,
    "get_shards": SHARDS_RESPONSE,
    "get_players": PLAYERS_RESPONSE,
    "get_player": PLAYER_RESPONSE,
    "get_player_inventory": INVENTORY_RESPONSE,
    "announce": BOOL_RESPONSE,
    "save": BOOL_RESPONSE,
    "set_server_paused": BOOL_RESPONSE,
    "reset": BOOL_RESPONSE,
    "regenerate_world": BOOL_RESPONSE,
    "regenerate_shard": BOOL_RESPONSE,
    "rollback": BOOL_RESPONSE,
    "kick_player": BOOL_RESPONSE,
    "ban_player": BOOL_RESPONSE,
    "get_blocklist": PLAYER_IDS_RESPONSE,
    "is_blocked": BOOL_RESPONSE,
    "unban_player": BOOL_RESPONSE,
    "is_whitelisted": BOOL_RESPONSE,
    "whitelist_player": BOOL_RESPONSE,
    "unwhitelist_player": BOOL_RESPONSE,
    "set_player_vitals": BOOL_RESPONSE,
    "kill_player": BOOL_RESPONSE,
    "revive_player": BOOL_RESPONSE,
    "despawn_player": BOOL_RESPONSE,
    "migrate_player": BOOL_RESPONSE,
    "teleport_player": BOOL_RESPONSE,
    "give_item": INT_RESPONSE,
    "remove_item": INT_RESPONSE,
    "execute_script": JSON_RESPONSE,
}


def run_lua_contract(script: str, luajit: str) -> list[str]:
    root = Path(__file__).parents[1]
    result = subprocess.run(  # ruff:ignore[subprocess-without-shell-equals-true]
        [
            luajit,
            str(root / f"tests/lua/{script}"),
            str(root / "src/dst_server/lua"),
            str(root / "tests/lua"),
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
    assert {event.event for event in events} == schema_events
    assert [event.seq for event in events] == list(range(1, 59))
    assert all(event.nonce == "01ARZ3NDEKTSV4RRFFQ69G5FAV" for event in events)


def test_all_real_lua_rpc_methods_match_python_contracts(luajit: str) -> None:
    lines = run_lua_contract("rpc_contract.lua", luajit)
    responses = dict(line.split("|", 1) for line in lines)

    assert responses.keys() == RPC_ADAPTERS.keys()
    for method, adapter in RPC_ADAPTERS.items():
        adapter.validate_json(responses[method], strict=True)

    players = PLAYERS_RESPONSE.validate_json(responses["get_players"], strict=True)
    loading_player = PLAYER_RESPONSE.validate_json(
        responses["get_player"],
        strict=True,
    )
    assert players.ok
    assert players.data[1].prefab is None
    assert loading_player.ok
    assert loading_player.data is not None
    assert loading_player.data.prefab is None

    blocklist = PLAYER_IDS_RESPONSE.validate_json(
        responses["get_blocklist"],
        strict=True,
    )
    assert blocklist.ok
    assert blocklist.data == ("KU_BLOCKED", "KU_KEEP", "Steam_ONLY")
    for method in (
        "is_blocked",
        "unban_player",
        "is_whitelisted",
        "whitelist_player",
        "unwhitelist_player",
    ):
        membership = BOOL_RESPONSE.validate_json(responses[method], strict=True)
        assert membership.ok
        assert membership.data is True
