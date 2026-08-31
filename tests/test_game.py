import math
from collections.abc import Awaitable, Callable
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from pydantic import ValidationError

from dst_server.game import GameClient
from dst_server.game.players import MAX_GIVE_ITEMS
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
from dst_server.models import Item, Stat
from dst_server.telemetry import TelemetrySettings
from dst_server.telemetry.recorder import Recorder
from tests.helpers import structured_result

type Invocation = Callable[[GameClient], Awaitable[object]]


def make_game(response: str = "") -> tuple[GameClient, list[str]]:
    commands: list[str] = []

    async def execute(command: str) -> str:  # ruff:ignore[unused-async]
        commands.append(command)
        return response

    async def execute_reload(  # ruff:ignore[unused-async]
        command: str,
        completion_timeout: float,
    ) -> tuple[str, int, float]:
        del completion_timeout
        commands.append(command)
        return response, 0, float("inf")

    async def wait_reload(  # ruff:ignore[unused-async]
        generation: int,
        deadline: float,
    ) -> None:
        del generation, deadline

    game = GameClient(
        shard="Master",
        lua_directory=Path("/lua"),
        telemetry=TelemetrySettings(),
        execute=execute,
        execute_ready=execute,
        execute_reload=execute_reload,
        wait_reload=wait_reload,
        recorder=Recorder("cluster", "Master"),
        session_id=lambda: "SESSION",
        nonce="01ARZ3NDEKTSV4RRFFQ69G5FAV",
    )
    return game, commands


ROUTES = [
    (lambda game: game.get_health(), "health", {}, DRIVER_RESPONSE),
    (lambda game: game.world.room(), "get_room", {}, ROOM_RESPONSE),
    (lambda game: game.world.state(), "get_world", {}, WORLD_RESPONSE),
    (lambda game: game.world.runtime(), "get_runtime", {}, RUNTIME_RESPONSE),
    (lambda game: game.world.mods(), "get_mods", {}, MODS_RESPONSE),
    (
        lambda game: game.world.shards(),
        "get_shards",
        {"current_name": "Master"},
        SHARDS_RESPONSE,
    ),
    (lambda game: game.players.list(), "get_players", {}, PLAYERS_RESPONSE),
    (
        lambda game: game.players.get("KU_TEST"),
        "get_player",
        {"userid": "KU_TEST"},
        PLAYER_RESPONSE,
    ),
    (
        lambda game: game.players.inventory("KU_TEST"),
        "get_player_inventory",
        {"userid": "KU_TEST"},
        INVENTORY_RESPONSE,
    ),
    (
        lambda game: game.world.announce("hello"),
        "announce",
        {"message": "hello"},
        BOOL_RESPONSE,
    ),
    (lambda game: game.world.request_save(), "save", {}, BOOL_RESPONSE),
    (
        lambda game: game.world.pause(True),
        "set_server_paused",
        {"paused": True},
        BOOL_RESPONSE,
    ),
    (lambda game: game.world.reset(), "reset", {}, BOOL_RESPONSE),
    (
        lambda game: game.world.regenerate(),
        "regenerate_world",
        {},
        BOOL_RESPONSE,
    ),
    (
        lambda game: game.world.regenerate_shard(preserve_settings=False),
        "regenerate_shard",
        {"preserve_settings": False},
        BOOL_RESPONSE,
    ),
    (
        lambda game: game.world.rollback(2),
        "rollback",
        {"count": 2},
        BOOL_RESPONSE,
    ),
    (
        lambda game: game.players.kick("KU_TEST"),
        "kick_player",
        {"userid": "KU_TEST"},
        BOOL_RESPONSE,
    ),
    (
        lambda game: game.players.ban("KU_TEST", seconds=60),
        "ban_player",
        {"userid": "KU_TEST", "seconds": 60},
        BOOL_RESPONSE,
    ),
    (lambda game: game.players.blocklist(), "get_blocklist", {}, PLAYER_IDS_RESPONSE),
    (
        lambda game: game.players.is_blocked("KU_TEST"),
        "is_blocked",
        {"userid": "KU_TEST"},
        BOOL_RESPONSE,
    ),
    (
        lambda game: game.players.unban("KU_TEST"),
        "unban_player",
        {"userid": "KU_TEST"},
        BOOL_RESPONSE,
    ),
    (
        lambda game: game.players.is_whitelisted("KU_TEST"),
        "is_whitelisted",
        {"userid": "KU_TEST"},
        BOOL_RESPONSE,
    ),
    (
        lambda game: game.players.whitelist("KU_TEST"),
        "whitelist_player",
        {"userid": "KU_TEST"},
        BOOL_RESPONSE,
    ),
    (
        lambda game: game.players.unwhitelist("KU_TEST"),
        "unwhitelist_player",
        {"userid": "KU_TEST"},
        BOOL_RESPONSE,
    ),
    (
        lambda game: game.players.set_vitals(
            "KU_TEST",
            health=0.5,
            sanity=1,
            temperature=25,
            moisture=0,
        ),
        "set_player_vitals",
        {
            "userid": "KU_TEST",
            "health": 0.5,
            "sanity": 1.0,
            "moisture": 0.0,
            "temperature": 25.0,
        },
        BOOL_RESPONSE,
    ),
    (
        lambda game: game.players.kill("KU_TEST"),
        "kill_player",
        {"userid": "KU_TEST"},
        BOOL_RESPONSE,
    ),
    (
        lambda game: game.players.revive("KU_TEST"),
        "revive_player",
        {"userid": "KU_TEST"},
        BOOL_RESPONSE,
    ),
    (
        lambda game: game.players.despawn("KU_TEST"),
        "despawn_player",
        {"userid": "KU_TEST"},
        BOOL_RESPONSE,
    ),
    (
        lambda game: game.players.migrate("KU_TEST", "2", 3),
        "migrate_player",
        {"userid": "KU_TEST", "shard_id": "2", "portal_id": 3},
        BOOL_RESPONSE,
    ),
    (
        lambda game: game.players.teleport("KU_TEST", 1, 0, -2.5),
        "teleport_player",
        {"userid": "KU_TEST", "x": 1.0, "y": 0.0, "z": -2.5},
        BOOL_RESPONSE,
    ),
    (
        lambda game: game.players.give("KU_TEST", "Twigs", 3),
        "give_item",
        {"userid": "KU_TEST", "prefab": "twigs", "count": 3},
        INT_RESPONSE,
    ),
    (
        lambda game: game.players.remove("KU_TEST", "Twigs", 2),
        "remove_item",
        {"userid": "KU_TEST", "prefab": "twigs", "count": 2},
        INT_RESPONSE,
    ),
    (
        lambda game: game.world.execute("return {answer=42}"),
        "execute_script",
        {"source": "return {answer=42}"},
        JSON_RESPONSE,
    ),
]

VOID_METHODS = {
    "announce",
    "save",
    "reset",
    "regenerate_world",
    "regenerate_shard",
    "rollback",
    "kick_player",
    "ban_player",
}
RELOAD_METHODS = {"reset", "regenerate_world", "regenerate_shard", "rollback"}


@pytest.mark.parametrize(
    ("invoke", "method", "arguments", "adapter"),
    ROUTES,
    ids=[method for _, method, _, _ in ROUTES],
)
async def test_public_api_routes_typed_requests(
    monkeypatch: pytest.MonkeyPatch,
    invoke: Invocation,
    method: str,
    arguments: dict[str, object],
    adapter: ResponseAdapter[object],
) -> None:
    game, _ = make_game()
    response: object = object()
    expected_player_count = 0
    if method == "get_room":
        response = SimpleNamespace(player_count=3)
        expected_player_count = 3
    elif method == "get_players":
        response = (object(), object())
        expected_player_count = 2
    request = AsyncMock(return_value=response)
    reload = AsyncMock(return_value=response)
    monkeypatch.setattr(game, "request", request)
    monkeypatch.setattr(game, "reload", reload)

    result = await invoke(game)

    if method in RELOAD_METHODS:
        reload.assert_awaited_once_with(method, arguments, adapter, 30.0)
        request.assert_not_awaited()
    else:
        request.assert_awaited_once_with(method, arguments, adapter)
        reload.assert_not_awaited()
    assert result is (None if method in VOID_METHODS else response)
    assert game.recorder.player_count == expected_player_count


async def test_request_escapes_untrusted_text_before_lua_execution() -> None:
    game, commands = make_game(structured_result(True))

    await game.world.announce('hello");Shutdown()--\n你好')

    (command,) = commands
    assert "\n" not in command
    assert "json.decode" in command
    assert "c_announce" not in command


@pytest.mark.parametrize(
    ("player", "expected"),
    [
        (None, None),
        (SimpleNamespace(userid="KU_TEST", admin=False), False),
        (SimpleNamespace(userid="KU_TEST", admin=True), True),
    ],
)
async def test_admin_query_is_limited_to_connected_players(
    monkeypatch: pytest.MonkeyPatch,
    player: object | None,
    expected: bool | None,
) -> None:
    game, _ = make_game()
    get_player = AsyncMock(return_value=player)
    monkeypatch.setattr(game.players, "get", get_player)

    assert await game.players.is_admin("KU_TEST") is expected
    get_player.assert_awaited_once_with("KU_TEST")


async def test_give_enforces_spawn_limit(monkeypatch: pytest.MonkeyPatch) -> None:
    game, _ = make_game()
    request = AsyncMock(return_value=MAX_GIVE_ITEMS)
    monkeypatch.setattr(game, "request", request)

    assert await game.players.give("KU_TEST", "Twigs", MAX_GIVE_ITEMS) == 64
    request.assert_awaited_once_with(
        "give_item",
        {"userid": "KU_TEST", "prefab": "twigs", "count": MAX_GIVE_ITEMS},
        INT_RESPONSE,
    )

    with pytest.raises(ValueError, match="must not exceed 64"):
        await game.players.give("KU_TEST", "twigs", MAX_GIVE_ITEMS + 1)


@pytest.mark.parametrize("value", [-1.0, 2.0])
def test_vital_percent_is_bounded(value: float) -> None:
    with pytest.raises(ValidationError):
        Stat.model_validate({"current": 1.0, "maximum": 1.0, "percent": value})


@pytest.mark.parametrize(
    "field",
    [
        "moisture_percent",
        "uses_percent",
        "freshness_percent",
        "fuel_percent",
        "armor_percent",
        "charge_percent",
    ],
)
@pytest.mark.parametrize("value", [-1.0, 2.0])
def test_item_percent_is_bounded(field: str, value: float) -> None:
    data: dict[str, object] = {
        "prefab": "twigs",
        "guid": 1,
        "skin": None,
        "stack_size": 1,
        "moisture_percent": None,
        "uses_percent": None,
        "freshness_percent": None,
        "fuel_percent": None,
        "armor_percent": None,
        "charge_percent": None,
    }

    with pytest.raises(ValidationError):
        Item.model_validate(data | {field: value})


@pytest.mark.parametrize(
    ("response", "error", "message"),
    [
        (
            'DST_SERVER_RESULT|{"ok":false,"error":"boom"}',
            RuntimeError,
            "boom",
        ),
        (
            'DST_SERVER_RESULT|{"ok":true,"data":1}',
            ValidationError,
            "bool_type",
        ),
        (
            'DST_SERVER_RESULT|{"ok":true,"data":true,"extra":1}',
            ValidationError,
            "extra_forbidden",
        ),
        ("unstructured output", RuntimeError, "structured result"),
    ],
)
async def test_response_contract_rejects_invalid_results(
    response: str,
    error: type[Exception],
    message: str,
) -> None:
    game, _ = make_game(response)

    with pytest.raises(error, match=message):
        await game.world.request_save()


@pytest.mark.parametrize(
    ("invoke", "error", "message"),
    [
        (lambda game: game.world.pause(1), TypeError, "boolean"),
        (
            lambda game: game.world.regenerate_shard(preserve_settings=1),
            TypeError,
            "boolean",
        ),
        (lambda game: game.world.rollback(-1), ValueError, "non-negative"),
        (lambda game: game.world.execute(""), ValueError, "must not be empty"),
        (
            lambda game: game.players.set_vitals("KU_TEST"),
            ValueError,
            "at least one",
        ),
        (
            lambda game: game.players.set_vitals("KU_TEST", health=2),
            ValueError,
            "between 0 and 1",
        ),
        (
            lambda game: game.players.set_vitals("KU_TEST", temperature=math.inf),
            ValueError,
            "finite",
        ),
        (
            lambda game: game.players.teleport("KU_TEST", math.nan, 0, 0),
            ValueError,
            "finite",
        ),
        (
            lambda game: game.players.give("KU_TEST", "twigs", 0),
            ValueError,
            "positive",
        ),
        (
            lambda game: game.players.remove("KU_TEST", ""),
            ValueError,
            "prefab",
        ),
        (lambda game: game.players.get(""), ValueError, "userid"),
    ],
    ids=range(11),
)
async def test_public_api_rejects_invalid_values(
    invoke: Invocation,
    error: type[Exception],
    message: str,
) -> None:
    game, _ = make_game()

    with pytest.raises(error, match=message):
        await invoke(game)
