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
    MAX_RESULT_LINE_BYTES,
    MODS_RESPONSE,
    PLAYER_IDS_RESPONSE,
    PLAYER_RESPONSE,
    PLAYERS_RESPONSE,
    RESULT_PREFIX,
    ROOM_RESPONSE,
    RUNTIME_RESPONSE,
    SHARDS_RESPONSE,
    SNAPSHOTS_RESPONSE,
    WORLD_RESPONSE,
    ResponseAdapter,
    Success,
)
from dst_server.models import Item, Stat
from dst_server.runtime import IndeterminateCommandError
from dst_server.telemetry import TelemetrySettings
from dst_server.telemetry.recorder import Recorder
from dst_server.timeouts import DEFAULT_RELOAD_TIMEOUT
from tests.helpers import run_lua, structured_result

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
    (
        lambda game: game.world.snapshots(23, before=101),
        "get_snapshots",
        {"limit": 23, "before": 101},
        SNAPSHOTS_RESPONSE,
    ),
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
        lambda game: game.world.rollback_to_snapshot("SESSION", 3),
        "rollback_to_snapshot",
        {"session_id": "SESSION", "snapshot_id": 3},
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
    "rollback_to_snapshot",
    "kick_player",
    "ban_player",
}
RELOAD_METHODS = {
    "reset",
    "regenerate_world",
    "regenerate_shard",
    "rollback",
    "rollback_to_snapshot",
}


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
        reload.assert_awaited_once_with(
            method, arguments, adapter, DEFAULT_RELOAD_TIMEOUT
        )
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
    assert "json.decode" not in command
    assert "c_announce" not in command


def test_native_snapshot_pages_cover_long_history(lua_runtime: str) -> None:
    output = run_lua(
        """
        TheWorld = { meta = { session_identifier = "SESSION" } }
        local calls = {}
        TheNet = {
            IsOnlineMode = function() return false end,
            ListSnapshots = function(_, session, online, count)
                assert(session == "SESSION" and online == false)
                calls[#calls + 1] = count
                local result = {}
                for index = 1, math.min(count, 237) do
                    local id = (238 - index) * 3
                    result[#result + 1] = {
                        snapshot_id = id,
                        world_file = string.format("session/SESSION/%010d", id),
                    }
                end
                result[1], result[#result] = result[#result], result[1]
                return result, count < 237
            end,
        }
        local query = require("dst_server.world_queries").get_snapshots
        local wire = require("dst_server.wire")
        local before = nil
        repeat
            local page = query({ limit = 100, before = before })
            wire.reply(function() return page end)
            if not page.has_more then break end
            before = page.snapshots[#page.snapshots].snapshot_id
        until false
        assert(calls[1] == 101 and calls[#calls] > 237)
        """,
        lua_runtime,
    )
    pages = []
    for line in output.decode().splitlines():
        assert len(line.encode()) <= MAX_RESULT_LINE_BYTES
        assert line.startswith(RESULT_PREFIX)
        result = SNAPSHOTS_RESPONSE.validate_json(line.removeprefix(RESULT_PREFIX))
        assert isinstance(result, Success)
        pages.append(result.data)
    assert [len(page.snapshots) for page in pages] == [100, 100, 37]
    assert [page.has_more for page in pages] == [True, True, False]
    assert [item.snapshot_id for page in pages for item in page.snapshots] == list(
        range(711, 0, -3)
    )


async def test_snapshot_catalog_retains_unavailable_world_files() -> None:
    game, _ = make_game(
        structured_result({
            "session_id": "SESSION",
            "snapshots": [{"snapshot_id": 0, "world_file": None}],
            "has_more": False,
        })
    )

    catalog = await game.world.snapshots()

    assert catalog.session_id == "SESSION"
    assert not catalog.has_more
    assert len(catalog.snapshots) == 1
    assert catalog.snapshots[0].snapshot_id == 0
    assert catalog.snapshots[0].world_file is None
    assert catalog.snapshots[0].metadata is None


@pytest.mark.parametrize(
    ("arguments", "message"),
    [
        ({"limit": 0}, "positive"),
        ({"limit": 101}, "must not exceed 100"),
        ({"before": -1}, "non-negative"),
    ],
)
async def test_snapshot_page_bounds(arguments: dict[str, int], message: str) -> None:
    game, commands = make_game()

    with pytest.raises(ValueError, match=message):
        await game.world.snapshots(**arguments)

    assert commands == []


async def test_indeterminate_lua_mutation_does_not_wait_for_reload(
    lua_runtime: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    output = run_lua(
        'local wire=require("dst_server.wire");wire.reply(wire.indeterminate)',
        lua_runtime,
    )
    game, _ = make_game(output.decode())
    wait = AsyncMock()
    monkeypatch.setattr(game, "wait_reload", wait)

    with pytest.raises(IndeterminateCommandError, match="may have been applied"):
        await game.world.rollback()

    wait.assert_not_awaited()


async def test_reload_wait_failure_is_indeterminate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    game, _ = make_game(structured_result(True))
    wait = AsyncMock(side_effect=EOFError("event stream closed"))
    monkeypatch.setattr(game, "wait_reload", wait)

    with pytest.raises(IndeterminateCommandError, match="could not be confirmed"):
        await game.world.rollback()

    wait.assert_awaited_once()


@pytest.mark.parametrize(
    "scenario",
    [
        "success",
        "gapped",
        "older",
        "missing",
        "session",
        "shard",
        "current",
        "future",
        "truncate",
        "noop",
        "reset",
    ],
)
async def test_native_snapshot_rollback_checks_target_and_partial_mutation(
    scenario: str, lua_runtime: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    output = run_lua(
        f'local scenario="{scenario}";'
        """
        TheWorld = {
            ismastershard = scenario ~= "shard",
            meta = { session_identifier = "SESSION" },
        }
        local truncated, reset = 0, 0
        local selected = "session/SESSION/0000000005"
        local current_snapshot = scenario == "older" and 454
            or scenario == "gapped" and 7
            or scenario == "current" and 3
            or scenario == "future" and 2 or 6
        local expected_offset = scenario == "older" and -451
            or scenario == "gapped" and -4 or -3
        TheNet = {
            IsOnlineMode = function() return true end,
            GetCurrentSnapshot = function() return current_snapshot end,
            ListSnapshots = function(_, session, online, count)
                assert(session == "SESSION" and online == true)
                local ids = { 5, 3, 2 }
                if scenario == "older" then
                    ids = {}
                    for index = 1, 151 do ids[index] = (152-index)*3 end
                elseif scenario == "gapped" then ids = { 6, 4, 3, 2 }
                elseif scenario == "missing" then ids = { 5, 2 } end
                local result = {}
                for index = 1, math.min(count, #ids) do
                    result[index] = {
                        snapshot_id = ids[index],
                        world_file = string.format("session/SESSION/%010d", ids[index]),
                    }
                end
                result[1], result[#result] = result[#result], result[1]
                return result, count < #ids
            end,
            TruncateSnapshots = function(_, session, offset)
                assert(session == "SESSION" and offset == expected_offset)
                truncated = truncated + 1
                if scenario == "truncate" then error("partial native failure") end
                if scenario ~= "noop" then selected = "session/SESSION/0000000003" end
            end,
            GetWorldSessionFile = function(_, session)
                assert(session == "SESSION")
                return selected
            end,
        }
        WorldRollbackFromSim = function(count)
            assert(count == 0 and selected == "session/SESSION/0000000003")
            reset = reset + 1
            if scenario == "reset" then error("reset failed after truncation") end
        end
        require("dst_server.wire").reply(function()
            return require("dst_server.commands").rollback_to_snapshot({
                session_id = scenario == "session" and "STALE" or "SESSION",
                snapshot_id = 3,
            })
        end)
        local rejected = scenario == "missing" or scenario == "session"
            or scenario == "shard" or scenario == "current" or scenario == "future"
        assert(truncated == (rejected and 0 or 1))
        local restarted = scenario == "success" or scenario == "gapped"
            or scenario == "older" or scenario == "reset"
        assert(reset == (restarted and 1 or 0))
        """,
        lua_runtime,
    )
    game, _ = make_game(output.decode())
    wait = AsyncMock()
    monkeypatch.setattr(game, "wait_reload", wait)

    if scenario in {"success", "gapped", "older"}:
        await game.world.rollback_to_snapshot("SESSION", 3)
        wait.assert_awaited_once()
    else:
        partial = scenario in {"truncate", "noop", "reset"}
        expected = IndeterminateCommandError if partial else RuntimeError
        message = "may have been applied" if partial else "lua_error"
        with pytest.raises(expected, match=message):
            await game.world.rollback_to_snapshot("SESSION", 3)
        wait.assert_not_awaited()


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
            'DST_SERVER_RESULT|{"ok":false,"error":"lua_error"}',
            RuntimeError,
            "lua_error",
        ),
        (
            'DST_SERVER_RESULT|{"ok":false,"error":"boom"}',
            ValidationError,
            "literal_error",
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
        (
            lambda game: game.world.rollback_to_snapshot("", 3),
            ValueError,
            "session_id",
        ),
        (
            lambda game: game.world.rollback_to_snapshot("SESSION", 0),
            ValueError,
            "positive",
        ),
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
    ids=range(13),
)
async def test_public_api_rejects_invalid_values(
    invoke: Invocation,
    error: type[Exception],
    message: str,
) -> None:
    game, _ = make_game()

    with pytest.raises(error, match=message):
        await invoke(game)
