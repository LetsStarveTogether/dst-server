from __future__ import annotations

from typing import TYPE_CHECKING

from pydantic import JsonValue

from dst_server.models import Inventory, Player

from .rpc import (
    BOOL_RESPONSE,
    INT_RESPONSE,
    INVENTORY_RESPONSE,
    PLAYER_IDS_RESPONSE,
    PLAYER_RESPONSE,
    PLAYERS_RESPONSE,
)
from .validation import (
    item_count,
    number,
    percent,
    player_id,
    prefab,
    required_string,
)

if TYPE_CHECKING:
    from .client import GameClient

MAX_GIVE_ITEMS = 64


class PlayerClient:  # ruff:ignore[too-many-public-methods]
    def __init__(self, game: GameClient) -> None:
        self.game = game

    async def list(self) -> tuple[Player, ...]:
        players = await self.game.request("get_players", {}, PLAYERS_RESPONSE)
        self.game.recorder.set_player_count(len(players))
        return players

    async def get(self, userid: str) -> Player | None:
        return await self.game.request(
            "get_player",
            {"userid": player_id(userid)},
            PLAYER_RESPONSE,
        )

    async def inventory(self, userid: str) -> Inventory | None:
        return await self.game.request(
            "get_player_inventory",
            {"userid": player_id(userid)},
            INVENTORY_RESPONSE,
        )

    async def kick(self, userid: str) -> None:
        await self.game.request(
            "kick_player",
            {"userid": player_id(userid)},
            BOOL_RESPONSE,
        )

    async def ban(self, userid: str, *, seconds: int | None = None) -> None:
        arguments: dict[str, JsonValue] = {"userid": player_id(userid)}
        if seconds is not None:
            arguments["seconds"] = item_count(seconds)
        await self.game.request("ban_player", arguments, BOOL_RESPONSE)

    async def blocklist(self) -> tuple[str, ...]:
        """Return this shard process's runtime blacklist identifiers."""
        return await self.game.request("get_blocklist", {}, PLAYER_IDS_RESPONSE)

    async def is_blocked(self, userid: str) -> bool:
        return await self.game.request(
            "is_blocked",
            {"userid": player_id(userid)},
            BOOL_RESPONSE,
        )

    async def unban(self, userid: str) -> bool:
        """Remove all matching entries and report whether any existed."""
        return await self.game.request(
            "unban_player",
            {"userid": player_id(userid)},
            BOOL_RESPONSE,
        )

    async def is_whitelisted(self, userid: str) -> bool:
        """Query whitelist membership on the master shard."""
        return await self.game.request(
            "is_whitelisted",
            {"userid": player_id(userid)},
            BOOL_RESPONSE,
        )

    async def whitelist(self, userid: str) -> bool:
        """Add a member on the master shard and report resulting membership."""
        return await self.game.request(
            "whitelist_player",
            {"userid": player_id(userid)},
            BOOL_RESPONSE,
        )

    async def unwhitelist(self, userid: str) -> bool:
        """Remove a member on the master shard and report resulting absence."""
        return await self.game.request(
            "unwhitelist_player",
            {"userid": player_id(userid)},
            BOOL_RESPONSE,
        )

    async def is_admin(self, userid: str) -> bool | None:
        """Return online admin state, or ``None`` when not connected."""
        player = await self.get(userid)
        return None if player is None else player.admin

    async def set_vitals(
        self,
        userid: str,
        *,
        health: float | None = None,
        hunger: float | None = None,
        sanity: float | None = None,
        temperature: float | None = None,
        moisture: float | None = None,
    ) -> bool:
        arguments: dict[str, JsonValue] = {"userid": player_id(userid)}
        percentages = {
            "health": health,
            "hunger": hunger,
            "sanity": sanity,
            "moisture": moisture,
        }
        arguments.update(
            (name, percent(name, value))
            for name, value in percentages.items()
            if value is not None
        )
        if temperature is not None:
            arguments["temperature"] = number("temperature", temperature)
        if len(arguments) == 1:
            msg = "at least one player vital must be supplied"
            raise ValueError(msg)
        return await self.game.request("set_player_vitals", arguments, BOOL_RESPONSE)

    async def kill(self, userid: str) -> bool:
        return await self.game.request(
            "kill_player",
            {"userid": player_id(userid)},
            BOOL_RESPONSE,
        )

    async def revive(self, userid: str) -> bool:
        return await self.game.request(
            "revive_player",
            {"userid": player_id(userid)},
            BOOL_RESPONSE,
        )

    async def despawn(self, userid: str) -> bool:
        return await self.game.request(
            "despawn_player",
            {"userid": player_id(userid)},
            BOOL_RESPONSE,
        )

    async def migrate(
        self,
        userid: str,
        shard_id: str,
        portal_id: int = 1,
    ) -> bool:
        return await self.game.request(
            "migrate_player",
            {
                "userid": player_id(userid),
                "shard_id": required_string("shard ID", shard_id),
                "portal_id": item_count(portal_id),
            },
            BOOL_RESPONSE,
        )

    async def teleport(
        self,
        userid: str,
        x: float,
        y: float,
        z: float,
    ) -> bool:
        return await self.game.request(
            "teleport_player",
            {
                "userid": player_id(userid),
                "x": number("x", x),
                "y": number("y", y),
                "z": number("z", z),
            },
            BOOL_RESPONSE,
        )

    async def give(self, userid: str, item: str, count: int = 1) -> int:
        count = item_count(count)
        if count > MAX_GIVE_ITEMS:
            msg = f"count must not exceed {MAX_GIVE_ITEMS}"
            raise ValueError(msg)
        return await self.game.request(
            "give_item",
            {
                "userid": player_id(userid),
                "prefab": prefab(item),
                "count": count,
            },
            INT_RESPONSE,
        )

    async def remove(self, userid: str, item: str, count: int = 1) -> int:
        return await self.game.request(
            "remove_item",
            {
                "userid": player_id(userid),
                "prefab": prefab(item),
                "count": item_count(count),
            },
            INT_RESPONSE,
        )


__all__ = ["MAX_GIVE_ITEMS", "PlayerClient"]
