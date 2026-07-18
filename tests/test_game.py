from __future__ import annotations

import math

import pytest
from pydantic import ValidationError

from tests.helpers import (
    StubServer,
    item_data,
    player_data,
    room_data,
    runtime_data,
    structured_result,
    world_data,
)


async def test_read_api_validates_complete_models() -> None:
    inventory = {
        "userid": "KU_TEST",
        "items": [{"slot": 1, "item": item_data()}],
        "equipment": [{"slot": "hands", "item": item_data("axe")}],
        "active_item": None,
        "overflow": {
            "prefab": "backpack",
            "guid": 101,
            "slots": [{"slot": 2, "item": item_data("rocks")}],
        },
    }
    server = StubServer([
        structured_result(room_data()),
        structured_result(world_data()),
        structured_result(runtime_data()),
        structured_result([
            {"id": "workshop-1", "name": "Test Mod", "version": "1.2.3"}
        ]),
        structured_result([
            {
                "id": "1",
                "name": "Master",
                "is_current": True,
                "ready": True,
                "tags": [],
            },
            {
                "id": "2",
                "name": "Caves",
                "is_current": False,
                "ready": True,
                "tags": ["cave"],
            },
        ]),
        structured_result([player_data()]),
        structured_result(player_data()),
        structured_result(inventory),
        structured_result(None),
    ])

    room = await server.game.world.room()
    world = await server.game.world.state()
    runtime = await server.game.world.runtime()
    mods = await server.game.world.mods()
    shards = await server.game.world.shards()
    players = await server.game.players.list()
    player = await server.game.players.get("KU_TEST")
    player_inventory = await server.game.players.inventory("KU_TEST")

    assert room.name == "Test Room"
    assert world.day == 11
    assert world.wetness == 73
    assert world.lunar_hail_level == 42
    assert runtime.snapshot == 26
    assert runtime.seed == 704793166
    assert mods[0].version == "1.2.3"
    assert shards[1].tags == ("cave",)
    assert players[0].position is not None
    assert players[0].position.x == 1
    assert players[0].vitals is not None
    assert players[0].vitals.health is not None
    assert players[0].vitals.health.percent == pytest.approx(0.8)
    assert players[0].state is not None
    assert players[0].state.combat_target is not None
    assert players[0].state.combat_target.prefab == "hound"
    assert players[0].state.activated_skills == ("wilson_torch_1",)
    assert player == players[0]
    assert player_inventory is not None
    assert player_inventory.items[0].item.stack_size == 12
    assert player_inventory.items[0].item.guid == 100
    assert player_inventory.items[0].item.charge_percent == 1
    assert player_inventory.overflow is not None
    assert player_inventory.overflow.slots[0].item.prefab == "rocks"
    assert await server.game.players.get("KU_MISSING") is None


async def test_write_api_builds_safe_lua() -> None:
    server = StubServer([
        *[structured_result(data=True) for _ in range(15)],
        structured_result(3),
        structured_result(2),
        structured_result({"answer": 42}),
    ])

    await server.game.world.announce('hello");Shutdown()--\n你好')
    await server.game.world.request_save()
    assert await server.game.world.pause(True) is True
    await server.game.world.reset()
    await server.game.world.regenerate()
    await server.game.world.regenerate_shard()
    await server.game.world.rollback(2)
    await server.game.players.kick("KU_TEST")
    await server.game.players.ban("KU_TEST", seconds=60)
    assert (
        await server.game.players.set_vitals(
            "KU_TEST",
            health=0.5,
            sanity=1,
            temperature=25,
            moisture=0,
        )
        is True
    )
    assert await server.game.players.kill("KU_TEST") is True
    assert await server.game.players.revive("KU_TEST") is True
    assert await server.game.players.despawn("KU_TEST") is True
    assert await server.game.players.migrate("KU_TEST", "2", 3) is True
    assert await server.game.players.teleport("KU_TEST", 1, 0, -2.5) is True
    assert await server.game.players.give("KU_TEST", "Twigs", 3) == 3
    assert await server.game.players.remove("KU_TEST", "Twigs", 2) == 2
    assert await server.game.world.execute("return {\nanswer = 42\n}") == {"answer": 42}

    announcement = next(
        command for command in server.commands if 'call("announce"' in command
    )
    assert "\n" not in announcement
    assert "json.decode" in announcement
    assert "c_announce" not in announcement
    assert all(
        "require" in command and ".call" in command for command in server.commands
    )
    assert any("temperature" in command for command in server.commands)
    assert any("twigs" in command for command in server.commands)
    assert any("migrate_player" in command for command in server.commands)


async def test_strict_response_validation() -> None:
    extra_room = room_data() | {"unexpected": True}
    wrong_room = room_data() | {"max_players": "6"}
    server = StubServer([
        structured_result(extra_room),
        structured_result(wrong_room),
        'DST_SERVER_RESULT|{"ok":false,"error":"boom"}',
        'DST_SERVER_RESULT|{"ok":true,"data":true,"extra":1}',
    ])

    with pytest.raises(ValidationError, match="extra_forbidden"):
        await server.game.world.room()
    with pytest.raises(ValidationError, match="int_type"):
        await server.game.world.room()
    with pytest.raises(RuntimeError, match="boom"):
        await server.game.world.request_save()
    with pytest.raises(ValidationError, match="extra_forbidden"):
        await server.game.world.request_save()


async def test_write_api_rejects_invalid_values() -> None:
    server = StubServer([])

    with pytest.raises(ValueError, match="between 0 and 1"):
        await server.game.players.set_vitals("KU_TEST", health=2)
    with pytest.raises(ValueError, match="finite"):
        await server.game.players.set_vitals("KU_TEST", temperature=math.inf)
    with pytest.raises(ValueError, match="finite"):
        await server.game.players.teleport("KU_TEST", math.nan, 0, 0)
    with pytest.raises(ValueError, match="positive"):
        await server.game.players.give("KU_TEST", "twigs", 0)
    with pytest.raises(ValueError, match="prefab"):
        await server.game.players.remove("KU_TEST", "")
    with pytest.raises(ValueError, match="userid"):
        await server.game.players.get("")
