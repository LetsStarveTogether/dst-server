from typing import TYPE_CHECKING

from pydantic import JsonValue

from dst_server.models import Mod, Room, Runtime, ShardStatus, World

from .rpc import (
    BOOL_RESPONSE,
    JSON_RESPONSE,
    MODS_RESPONSE,
    ROOM_RESPONSE,
    RUNTIME_RESPONSE,
    SHARDS_RESPONSE,
    WORLD_RESPONSE,
)
from .validation import item_count, positive_timeout

if TYPE_CHECKING:
    from .client import GameClient


class WorldClient:
    def __init__(self, game: GameClient) -> None:
        self.game = game

    async def room(self) -> Room:
        room = await self.game.request("get_room", {}, ROOM_RESPONSE)
        self.game.recorder.set_player_count(room.player_count)
        return room

    async def state(self) -> World:
        return await self.game.request("get_world", {}, WORLD_RESPONSE)

    async def runtime(self) -> Runtime:
        return await self.game.request("get_runtime", {}, RUNTIME_RESPONSE)

    async def mods(self) -> tuple[Mod, ...]:
        return await self.game.request("get_mods", {}, MODS_RESPONSE)

    async def shards(self) -> tuple[ShardStatus, ...]:
        return await self.game.request(
            "get_shards",
            {"current_name": self.game.shard},
            SHARDS_RESPONSE,
        )

    async def announce(self, message: str) -> None:
        await self.game.request("announce", {"message": message}, BOOL_RESPONSE)

    async def request_save(self) -> None:
        await self.game.request("save", {}, BOOL_RESPONSE)

    async def pause(self, paused: bool) -> bool:
        if not isinstance(paused, bool):
            msg = "paused must be a boolean"
            raise TypeError(msg)
        return await self.game.request(
            "set_server_paused",
            {"paused": paused},
            BOOL_RESPONSE,
        )

    async def reset(self, completion_timeout: float = 30) -> None:
        await self.game.reload(
            "reset",
            {},
            BOOL_RESPONSE,
            positive_timeout(completion_timeout),
        )

    async def regenerate(self, completion_timeout: float = 30) -> None:
        await self.game.reload(
            "regenerate_world",
            {},
            BOOL_RESPONSE,
            positive_timeout(completion_timeout),
        )

    async def regenerate_shard(
        self,
        *,
        preserve_settings: bool = True,
        completion_timeout: float = 30,
    ) -> None:
        if not isinstance(preserve_settings, bool):
            msg = "preserve_settings must be a boolean"
            raise TypeError(msg)
        await self.game.reload(
            "regenerate_shard",
            {"preserve_settings": preserve_settings},
            BOOL_RESPONSE,
            positive_timeout(completion_timeout),
        )

    async def rollback(self, count: int = 1, *, completion_timeout: float = 30) -> None:
        await self.game.reload(
            "rollback",
            {"count": item_count(count, allow_zero=True)},
            BOOL_RESPONSE,
            positive_timeout(completion_timeout),
        )

    async def execute(self, source: str) -> JsonValue:
        if not isinstance(source, str) or not source:
            msg = "Lua source must not be empty"
            raise ValueError(msg)
        return await self.game.request(
            "execute_script",
            {"source": source},
            JSON_RESPONSE,
        )
