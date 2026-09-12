from pathlib import Path

import pytest

from dst_server import commands as c
from dst_server.errors import IndeterminateCommandError
from tests.game.helpers import make_game
from tests.lua.helpers import run_lua


async def test_game_boundary_rejects_lifecycle_and_copied_invalid_commands() -> None:
    game, executed = make_game()
    with pytest.raises(ValueError, match="game"):
        await game.invoke(c.Start())
    with pytest.raises(ValueError, match="game"):
        await game.invoke(c.Save())
    with pytest.raises(ValueError, match="count"):
        await game.invoke(
            c.Give(userid="KU_TEST", item="twigs").model_copy(update={"count": 65})
        )
    assert executed == []


async def test_game_read_deadline_cancels_waiting_query(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import asyncio

    cancelled = asyncio.Event()

    async def query(*_: object) -> None:
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    game, _ = make_game()
    monkeypatch.setattr(game, "request", query)
    with pytest.raises(TimeoutError):
        await game.invoke(c.World(timeout=0.01))
    assert cancelled.is_set()


@pytest.mark.parametrize(
    "response",
    [
        b"unstructured output",
        b'{"ok":true,"data":1}',
        b'{"ok":true,"data":true,"data":false}',
        b'{"ok":false,"error":"invalid_utf8"}',
    ],
)
async def test_save_with_unconfirmed_native_ack_is_indeterminate(
    response: bytes,
) -> None:
    game, executed = make_game(response)
    with pytest.raises(IndeterminateCommandError, match="could not be confirmed"):
        await game.request_save()
    assert len(executed) == 1


async def test_arbitrary_lua_error_is_indeterminate_after_execution() -> None:
    game, executed = make_game(b'{"ok":false,"error":"lua_error"}')
    with pytest.raises(IndeterminateCommandError, match="could not be confirmed"):
        await game.invoke(
            c.ExecuteJson(source='c_announce("changed"); error("failed")')
        )
    assert len(executed) == 1


@pytest.mark.parametrize(
    "scenario",
    ["saved", "unchanged", "rewound", "session", "world", "secondary", "error"],
)
def test_native_save_requires_its_own_completion(
    native_scripts: Path,
    scenario: str,
    lua_runtime: str,
) -> None:
    run_lua(
        f'local scenario="{scenario}";'
        """
        local commands = require("dst_server.commands")
        local wire = require("dst_server.wire")
        local invoked, completed, failed, response, coordinated = 0, nil, nil, nil, nil
        local snapshot, queried = 27, false
        TheWorld = {
            ismastershard = scenario ~= "secondary",
            meta = {session_identifier = "SESSION"},
            PushEvent = function(_, name, data)
                assert(name == "master_autosaverupdate")
                coordinated = data.snapshot
            end,
        }
        TheNet = {
            GetCurrentSnapshot = function() return snapshot end,
            GetWorldSessionFile = function()
                queried = true
                error("save must not select a snapshot for loading")
            end,
        }
        ShardGameIndex = {SaveCurrent = function(_, callback)
            invoked = invoked + 1
            completed = callback
            if scenario == "error" then error("unknown partial mutation") end
        end}
        local function callback(data, failure) response, failed = data, failure end
        local initial = json.decode(wire.response(function()
            commands.save({}, callback)
            return true
        end))
        if scenario == "secondary" then
            assert(initial.ok == false and invoked == 0 and coordinated == nil)
            return
        end
        assert(invoked == 1 and coordinated == 27)
        assert(response == nil)
        if scenario == "error" then
            assert(initial.ok == false and initial.error == "indeterminate")
        else
            assert(initial.ok == true)
        end
        local rejected = json.decode(wire.response(function()
            commands.save({}, callback)
        end))
        assert(rejected.ok == false and invoked == 1)
        if scenario == "world" then TheWorld = {} end
        if scenario == "session" then TheWorld.meta.session_identifier = "OTHER" end
        snapshot = scenario == "unchanged" and 27 or scenario == "rewound" and 26 or 28
        completed()
        assert(not queried)
        if scenario == "unchanged" or scenario == "rewound"
            or scenario == "session" or scenario == "world" then
            assert(response == nil and failed == "indeterminate")
        else
            assert(failed == nil and response.snapshot == "session/SESSION/0000000027")
        end
        if scenario == "saved" then
            response = nil
            commands.save({}, callback)
            assert(invoked == 2 and coordinated == 28 and response == nil)
            snapshot = 29
            completed()
            assert(not queried and failed == nil)
            assert(response.snapshot == "session/SESSION/0000000028")
            assert(TheNet:GetCurrentSnapshot() == 29)
        end
        """,
        lua_runtime,
        native_scripts,
    )
