from unittest.mock import AsyncMock

import pytest

from dst_server import commands as c
from dst_server.errors import IndeterminateCommandError
from tests.game.test_client import make_game


async def test_game_boundary_accepts_shared_requests(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    game, _ = make_game()
    request = AsyncMock(return_value=3)
    monkeypatch.setattr(game, "request", request)
    assert await game.invoke(c.Give(userid="KU_TEST", item="Twigs", count=3)) == 3
    assert request.await_args is not None
    assert request.await_args.args[:2] == (
        "give_item",
        {"userid": "KU_TEST", "prefab": "twigs", "count": 3},
    )


async def test_game_boundary_rejects_lifecycle_and_copied_invalid_commands() -> None:
    game, executed = make_game()
    with pytest.raises(ValueError, match="game"):
        await game.invoke(c.Start())
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
        "unstructured output",
        'DST_SERVER_RESULT|{"ok":true,"data":1}',
        'DST_SERVER_RESULT|{"ok":true,"data":true,"data":false}',
        'DST_SERVER_RESULT|{"ok":false,"error":"invalid_utf8"}',
    ],
)
async def test_save_with_unconfirmed_native_ack_is_indeterminate(response: str) -> None:
    game, executed = make_game(response)
    with pytest.raises(IndeterminateCommandError, match="could not be confirmed"):
        await game.request_save()
    assert len(executed) == 1


async def test_arbitrary_lua_error_is_indeterminate_after_execution() -> None:
    game, executed = make_game('DST_SERVER_RESULT|{"ok":false,"error":"lua_error"}')
    with pytest.raises(IndeterminateCommandError, match="could not be confirmed"):
        await game.invoke(
            c.ExecuteJson(source='c_announce("changed"); error("failed")')
        )
    assert len(executed) == 1
