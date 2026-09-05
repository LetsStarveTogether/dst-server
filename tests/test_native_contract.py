import json
import subprocess  # ruff:ignore[suspicious-subprocess-import]
from pathlib import Path

import pytest
from luaparser import ast
from luaparser.astnodes import Call, Function, LocalFunction, Name, String

from dst_server.events import GAME_EVENT_ADAPTER

POSITION = {"x": 1, "y": 0, "z": 2}
PLAYER = {
    "prefab": "wilson",
    "guid": 101,
    "userid": "KU_PLAYER",
    "position": POSITION,
}
ATTACKER = {**PLAYER, "prefab": "wendy", "guid": 102, "userid": "KU_ATTACKER"}
TARGET = {**PLAYER, "prefab": "campfire", "guid": 201, "userid": None}
TWIGS = {"prefab": "twigs", "guid": 301, "skin": None, "stack_size": 1}
FLINT = {**TWIGS, "prefab": "flint", "guid": 302}
CAUSED = {"player": PLAYER, "caused_by_action_sequence": None}
COMBAT = {
    **CAUSED,
    "damage": None,
    "weapon": None,
    "stimuli": None,
    "special_damage": [],
}
ACTION = {
    "action_id": "DEPLOY",
    "action_sequence": 1,
    "actor": PLAYER,
    "target": None,
    "initial_target_owner": None,
    "inventory_object": None,
    "position": None,
    "recipe": None,
    "forced": False,
    "success": True,
    "reason": None,
    "error": None,
}

type ExpectedEvents = list[tuple[str, dict[str, object]]]

CASES: dict[str, ExpectedEvents] = {
    "action_platform_removed": [("action", {**ACTION, "success": False})],
    "action_platform_moved": [
        ("action", {**ACTION, "position": {"x": 110, "y": 0, "z": 120}})
    ],
    "action_ground": [("action", {**ACTION, "position": {"x": 10, "y": 0, "z": 20}})],
    "action_without_position": [("action", ACTION)],
    "work_without_action": [
        ("finished_work", {**CAUSED, "target": TARGET, "action_id": None})
    ],
    "work_with_action": [
        ("finished_work", {**CAUSED, "target": TARGET, "action_id": "CHOP"})
    ],
    "work_removed_position": [
        (
            "finished_work",
            {**CAUSED, "target": {**TARGET, "position": None}, "action_id": "CHOP"},
        )
    ],
    "unequip_empty": [
        ("unequipped", {**CAUSED, "item": None, "slot": "hands", "slip": False})
    ],
    "unequip_item": [
        (
            "unequipped",
            {
                **CAUSED,
                "item": {**TWIGS, "stack_size": 3},
                "slot": "hands",
                "slip": True,
            },
        )
    ],
    "migrate_number": [
        (
            "migration_started",
            {
                "player": PLAYER,
                "portal_id": 7,
                "destination_shard_id": "2",
                "destination": None,
            },
        )
    ],
    "migrate_string": [
        (
            "migration_started",
            {
                "player": PLAYER,
                "portal_id": "oceanwhirlbigportal",
                "destination_shard_id": "2",
                "destination": None,
            },
        )
    ],
    "migrate_without_destination": [],
    "combat_without_damage": [
        (
            "combat_received",
            {
                **COMBAT,
                "attacker": ATTACKER,
                "damage_resolved": 0,
                "original_damage": None,
                "redirected": None,
            },
        ),
        (
            "combat_hit",
            {
                **COMBAT,
                "player": ATTACKER,
                "target": PLAYER,
                "damage_resolved": 0,
                "redirected": None,
            },
        ),
    ],
    "combat_resolved_damage": [
        (
            "combat_received",
            {
                **COMBAT,
                "damage": 10,
                "attacker": ATTACKER,
                "damage_resolved": 7.5,
                "original_damage": 10,
                "redirected": None,
            },
        ),
        (
            "combat_hit",
            {
                **COMBAT,
                "damage": 10,
                "player": ATTACKER,
                "target": PLAYER,
                "damage_resolved": 7.5,
                "redirected": None,
            },
        ),
    ],
    "combat_blocked": [
        (
            "combat_blocked",
            {**COMBAT, "damage": 10, "attacker": ATTACKER, "original_damage": 10},
        )
    ],
    "drown_ocean": [("incident", {"player": PLAYER, "kind": "sink"})],
    "drown_void": [
        (
            "incident",
            {"player": PLAYER, "kind": "fall_in_void"},
        )
    ],
    "drown_safe": [],
    "drown_boat": [("incident", {"player": PLAYER, "kind": "sink"})],
    "drown_ocean_rejected": [],
    "drown_void_rejected": [],
    "drown_weregoose": [],
    "direct_sink": [("incident", {"player": PLAYER, "kind": "sink"})],
    "direct_fall": [("incident", {"player": PLAYER, "kind": "fall_in_void"})],
    "eat_soul": [
        (
            "ate",
            {
                **CAUSED,
                "food": {**TWIGS, "prefab": "wortox_soul", "guid": 401},
                "feeder": None,
            },
        )
    ],
    "spawn_fixed": [("spawned", {"player": {**PLAYER, "position": None}})],
    "spawn_scatter": [("spawned", {"player": {**PLAYER, "position": None}})],
    "pick_single": [("picked", {**CAUSED, "source": TARGET, "loot": [TWIGS]})],
    "pick_stack": [
        ("picked", {**CAUSED, "source": TARGET, "loot": [{**TWIGS, "stack_size": 3}]})
    ],
    "pick_array": [("picked", {**CAUSED, "source": TARGET, "loot": [TWIGS, FLINT]})],
    "pick_empty_array": [],
    "pick_missing_product": [],
    "pick_failed_spawn": [],
}


@pytest.fixture(scope="module")
def native_handlers(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """Extract shipped callbacks without loading their unrelated prefab dependencies."""
    native_root = Path(__file__).parents[1] / "dst-scripts/scripts"
    graph = ast.parse((native_root / "stategraphs/SGwilson.lua").read_text())
    handlers = [
        node
        for node in ast.walk(graph)
        if isinstance(node, Call)
        and isinstance(node.func, Name)
        and node.func.id == "EventHandler"
        and isinstance(node.args[0], String)
        and node.args[0].s in {b"onsink", b"onfallinvoid"}
    ]
    assert len(handlers) == 2
    definitions = []
    for filename, name in (
        ("prefabs/player_common.lua", "OnNewSpawn"),
        ("networking.lua", "SpawnNewPlayerOnServerFromSim"),
    ):
        tree = ast.parse((native_root / filename).read_text())
        matches = [
            node
            for node in tree.body.body
            if isinstance(node, (Function, LocalFunction))
            and isinstance(node.name, Name)
            and node.name.id == name
        ]
        assert len(matches) == 1
        definitions.append(ast.to_lua_source(matches[0]))
    path = tmp_path_factory.mktemp("native_handlers") / "handlers.lua"
    path.write_text(
        "local ex_fns = { GivePlayerStartingItems = function() end }\n"
        + "\n".join(definitions)
        + "\nreturn { on_new_spawn = OnNewSpawn, "
        "spawn_new_player = SpawnNewPlayerOnServerFromSim, events = {\n"
        + ",\n".join(ast.to_lua_source(node) for node in handlers)
        + "\n} }\n"
    )
    return path


@pytest.mark.parametrize(
    ("case", "profile", "expected"),
    [(case, "history", expected) for case, expected in CASES.items()]
    + [
        ("drown_ocean", "critical", CASES["drown_ocean"]),
        ("spawn_scatter", "critical", CASES["spawn_scatter"]),
        ("eat_soul", "critical", []),
        ("drown_ocean", "off", []),
        ("spawn_scatter", "off", []),
        ("eat_soul", "off", []),
    ],
)
def test_native_components_emit_valid_events(
    luajit: str,
    native_handlers: Path,
    case: str,
    profile: str,
    expected: ExpectedEvents,
) -> None:
    root = Path(__file__).parents[1]
    result = subprocess.run(  # ruff:ignore[subprocess-without-shell-equals-true]
        [
            luajit,
            str(root / "tests/lua/native_event_contract.lua"),
            str(root / "src/dst_server/lua"),
            str(root / "dst-scripts/scripts"),
            case,
            str(native_handlers),
            profile,
        ],
        capture_output=True,
        text=True,
        timeout=5,
        check=False,
    )
    assert result.returncode == 0, result.stderr or result.stdout
    lines = result.stdout.splitlines()
    health = [
        json.loads(line.split("|", 1)[1])
        for line in lines
        if line.startswith("NATIVE_HEALTH|")
    ]
    assert len(health) == 1
    assert health[0]["errors"] == 0, result.stdout
    events = [
        GAME_EVENT_ADAPTER.validate_json(line.removeprefix("DST_OTEL|"), strict=True)
        for line in lines
        if line.startswith("DST_OTEL|")
    ]
    assert len(events) == len(expected), result.stdout
    assert health[0]["events_emitted"] == len(expected)
    for sequence, (event, (name, data)) in enumerate(
        zip(events, expected, strict=True), 1
    ):
        assert event.model_dump(mode="json") == {
            "v": 2,
            "nonce": "01ARZ3NDEKTSV4RRFFQ69G5FAV",
            "generation": 1,
            "session_id": "SESSION",
            "seq": sequence,
            "event": f"dst.player.{name}",
            "tick": 10,
            "monotonic_ms": 20,
            "cycle": 3,
            "data": data,
        }
