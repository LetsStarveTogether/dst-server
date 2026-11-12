"""Python bindings for the native DST room SDK."""

lazy from collections.abc import Mapping
lazy from os import PathLike
lazy from typing import Any, Literal, Self

lazy from ._native import Client as _Client
lazy from ._native import DstError, describe, inspect
lazy from ._native import Subscription as _Subscription
lazy from .external import KleiClient
lazy from .host import Host
lazy from .logs import (
    JournalCursorError,
    JournalLogs,
    JournalQuery,
    JournalRecord,
    JournalResult,
    JournalStream,
    LogProcessError,
    NetdataLogQuery,
    NetdataLogRecord,
    NetdataLogResult,
    NetdataLogs,
)
lazy from .management import Permissions, VoteCommand, VoteStatus

__all__ = [
    "Client",
    "DstError",
    "Host",
    "JournalCursorError",
    "JournalLogs",
    "JournalQuery",
    "JournalRecord",
    "JournalResult",
    "JournalStream",
    "KleiClient",
    "LogProcessError",
    "NetdataLogQuery",
    "NetdataLogRecord",
    "NetdataLogResult",
    "NetdataLogs",
    "Permissions",
    "Subscription",
    "VoteCommand",
    "VoteStatus",
    "describe",
    "inspect",
]


class Client:  # ruff: ignore[too-many-public-methods]
    """A connection to one Agent; accepted operations survive cancelled waits."""

    __slots__ = ("_client",)

    def __init__(self, client: _Client) -> None:
        self._client = client

    @classmethod
    async def connect(cls, path: str | PathLike[str]) -> Self:
        return cls(await _Client.connect(path))

    async def call(
        self,
        method: str,
        arguments: Mapping[str, Any] | None = None,
        *,
        shard: str | None = None,
        timeout: float | None = None,
    ) -> Any:
        return await self._client.call(method, arguments, shard=shard, timeout=timeout)

    async def kick(self, userid: str) -> bool:
        """Disconnect a client, including character selection and loading."""
        return await self.call("kick", {"userid": userid})

    async def ban(self, userid: str, *, seconds: int | None = None) -> None:
        """Set a ban; seconds=None replaces any timed ban with a permanent ban."""
        await self.call("ban", {"userid": userid, "seconds": seconds})

    async def unban(self, userid: str) -> None:
        """Remove a permanent ban; native temporary bans expire on their own."""
        await self.call("unban", {"userid": userid})

    async def whitelist(self, userid: str) -> None:
        await self.call("whitelist", {"userid": userid})

    async def unwhitelist(self, userid: str) -> None:
        await self.call("unwhitelist", {"userid": userid})

    async def set_admin(self, userid: str, *, enabled: bool) -> None:
        await self.call("set_admin", {"userid": userid, "enabled": enabled})

    async def is_blocked(self, userid: str) -> bool:
        """Read permanent blacklist membership; native temporary bans are excluded."""
        return await self.call("is_blocked", {"userid": userid})

    async def is_admin(self, userid: str) -> bool:
        return await self.call("is_admin", {"userid": userid})

    async def is_whitelisted(self, userid: str) -> bool:
        return await self.call("is_whitelisted", {"userid": userid})

    async def blocklist(self) -> list[str]:
        """Read permanent blacklist membership; native temporary bans are excluded."""
        return await self.call("blocklist")

    async def read_permissions(self) -> Permissions:
        """Read stored permission IDs; temporary bans are not in these files."""
        return await self.call("read_permissions")

    async def list_vote_commands(self) -> list[VoteCommand]:
        return await self.call("list_vote_commands")

    async def get_vote(self) -> VoteStatus:
        return await self.call("get_vote")

    async def start_vote(
        self, command: str, *, starter_userid: str, target_userid: str | None = None
    ) -> VoteStatus:
        """Start a vote using native access, player count and cooldown rules."""
        return await self.call(
            "start_vote",
            {
                "command": command,
                "starter_userid": starter_userid,
                "target_userid": target_userid,
            },
        )

    async def cancel_vote(self) -> bool:
        """Cancel the current vote; return False when no vote is active."""
        return await self.call("cancel_vote")

    async def describe(self) -> dict[str, Any]:
        return await self._client.describe()

    async def subscribe(
        self, kind: Literal["logs", "lifecycle", "events"]
    ) -> Subscription:
        return Subscription(await self._client.subscribe(kind))

    async def close(self) -> None:
        await self._client.close()

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *_: object) -> None:
        await self.close()


class Subscription:
    __slots__ = ("_subscription",)

    def __init__(self, subscription: _Subscription) -> None:
        self._subscription = subscription

    async def next(self, max_items: int = 512) -> dict[str, Any]:
        return await self._subscription.next(max_items)

    async def close(self) -> None:
        await self._subscription.close()

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *_: object) -> None:
        await self.close()
