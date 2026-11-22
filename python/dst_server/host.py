# ruff: file-ignore[async-function-with-timeout]
"""Host operations delegated to the native SDK."""

import builtins
from contextlib import asynccontextmanager
lazy from collections.abc import AsyncGenerator, Mapping, Sequence
lazy from os import PathLike
lazy from typing import Any, Self

lazy import dst_server

lazy from ._native import Host as _Host
lazy from .logs import JournalQuery, JournalResult, JournalStream, _journal_result
lazy from .management import Permissions, VoteCommand, VoteStatus
lazy from .settings import Room, RoomStore


class Host:  # ruff: ignore[too-many-public-methods]
    """Manage room services and native configuration from one host.

    Submitted systemd jobs and accepted Agent commands may finish after a
    cancelled await; read status before retrying a mutation.
    """

    __slots__ = ("_host",)

    def __init__(
        self,
        root: str | PathLike[str],
        quadlet_dir: str | PathLike[str],
        *,
        systemctl: str | PathLike[str] | None = None,
        journalctl: str | PathLike[str] | None = None,
        user: bool = False,
        command_timeout: float = 30.0,
        port_start: int = 30000,
        port_end: int = 65535,
    ) -> None:
        self._host = _Host(
            root,
            quadlet_dir,
            systemctl=systemctl,
            journalctl=journalctl,
            user=user,
            command_timeout=command_timeout,
            port_start=port_start,
            port_end=port_end,
        )

    @property
    def user(self) -> bool:
        """Whether the room services belong to the current user's systemd manager."""
        return self._host.user

    @property
    def rooms(self) -> RoomStore:
        return self._host.rooms

    def units(self, number: int) -> builtins.list[str]:
        return self._host.units(number)

    async def list(self) -> builtins.list[dict[str, Any]]:
        """Return a batch outcome for each room, ordered by room number."""
        return await self._host.list()

    async def load(self, number: int) -> Room:
        return await self._host.load(number)

    async def create(self, definition: Room) -> Room:
        return await self._host.create(definition)

    async def edit(self, definition: Room) -> Room:
        return await self._host.edit(definition)

    async def edit_fields(
        self,
        number: int,
        changes: Sequence[tuple[str, Any]] = (),
        *,
        unset: Sequence[str] = (),
    ) -> Room:
        return await self._host.edit_fields(number, list(changes), unset)

    async def provision(
        self, definitions: Sequence[Room]
    ) -> builtins.list[dict[str, Any]]:
        return await self._host.provision(definitions)

    async def operate(self, number: int, request: Mapping[str, Any]) -> Any:
        return await self._host.operate(number, request)

    async def batch(
        self, numbers: Sequence[int], request: Mapping[str, Any]
    ) -> builtins.list[dict[str, Any]]:
        """Return one result per room, in input order, with up to eight in flight."""
        return await self._host.batch(numbers, request)

    async def status(self, number: int, *, game: bool = True) -> dict[str, Any]:
        return await self._host.operate(number, {"operation": "status", "game": game})

    async def diagnose(self, number: int) -> dict[str, Any]:
        return await self._host.operate(number, {"operation": "diagnose"})

    async def start(
        self, number: int, *, wait: bool = True, timeout: float = 900.0
    ) -> dict[str, Any]:
        return await self._host.operate(
            number, {"operation": "start", "wait": wait, "timeout": timeout}
        )

    async def stop(
        self, number: int, *, wait: bool = True, timeout: float = 900.0
    ) -> dict[str, Any]:
        return await self._host.operate(
            number, {"operation": "stop", "wait": wait, "timeout": timeout}
        )

    async def restart(
        self, number: int, *, wait: bool = True, timeout: float = 900.0
    ) -> dict[str, Any]:
        return await self._host.operate(
            number, {"operation": "restart", "wait": wait, "timeout": timeout}
        )

    async def wait_ready(
        self, number: int, *, timeout: float = 900.0
    ) -> dict[str, Any]:
        return await self._host.operate(
            number, {"operation": "wait_ready", "timeout": timeout}
        )

    async def announce(
        self, number: int, message: str, *, count: int = 1, interval: float = 1.0
    ) -> Any:
        return await self._host.operate(
            number,
            {
                "operation": "announce",
                "message": message,
                "count": count,
                "interval": interval,
            },
        )

    async def update_mods(
        self,
        number: int,
        *,
        restart: bool = False,
        notice: Mapping[str, Any] | None = None,
    ) -> Any:
        return await self._host.operate(
            number,
            {"operation": "update_mods", "restart": restart, "notice": notice},
        )

    async def set_policy(self, number: int, policy: Mapping[str, Any]) -> None:
        await self._host.operate(number, {"operation": "set_policy", "policy": policy})

    async def call(
        self, number: int, method: str, arguments: Mapping[str, Any] | None = None
    ) -> Any:
        return await self._host.operate(
            number,
            {
                "operation": "call",
                "request": {"method": method, "arguments": arguments or {}},
            },
        )

    async def kick(self, number: int, userid: str) -> bool:
        """Disconnect a client, including character selection and loading."""
        return await self.call(number, "kick", {"userid": userid})

    async def ban(
        self, number: int, userid: str, *, seconds: int | None = None
    ) -> None:
        """Set a ban; seconds=None replaces any timed ban with a permanent ban."""
        await self.call(number, "ban", {"userid": userid, "seconds": seconds})

    async def unban(self, number: int, userid: str) -> None:
        """Remove a permanent ban; native temporary bans expire on their own."""
        await self.call(number, "unban", {"userid": userid})

    async def whitelist(self, number: int, userid: str) -> None:
        await self.call(number, "whitelist", {"userid": userid})

    async def unwhitelist(self, number: int, userid: str) -> None:
        await self.call(number, "unwhitelist", {"userid": userid})

    async def set_admin(self, number: int, userid: str, *, enabled: bool) -> None:
        await self.call(number, "set_admin", {"userid": userid, "enabled": enabled})

    async def is_blocked(self, number: int, userid: str) -> bool:
        """Read permanent blacklist membership; native temporary bans are excluded."""
        return await self.call(number, "is_blocked", {"userid": userid})

    async def is_admin(self, number: int, userid: str) -> bool:
        return await self.call(number, "is_admin", {"userid": userid})

    async def is_whitelisted(self, number: int, userid: str) -> bool:
        return await self.call(number, "is_whitelisted", {"userid": userid})

    async def blocklist(self, number: int) -> builtins.list[str]:
        """Read permanent blacklist membership; native temporary bans are excluded."""
        return await self.call(number, "blocklist")

    async def read_permissions(self, number: int) -> Permissions:
        """Read stored permission IDs; temporary bans are not in these files."""
        return await self.call(number, "read_permissions")

    async def list_vote_commands(self, number: int) -> builtins.list[VoteCommand]:
        return await self.call(number, "list_vote_commands")

    async def get_vote(self, number: int) -> VoteStatus:
        return await self.call(number, "get_vote")

    async def start_vote(
        self,
        number: int,
        command: str,
        *,
        starter_userid: str,
        target_userid: str | None = None,
    ) -> VoteStatus:
        """Start a vote using native access, player count and cooldown rules."""
        return await self.call(
            number,
            "start_vote",
            {
                "command": command,
                "starter_userid": starter_userid,
                "target_userid": target_userid,
            },
        )

    async def cancel_vote(self, number: int) -> bool:
        """Cancel the current vote; return False when no vote is active."""
        return await self.call(number, "cancel_vote")

    @asynccontextmanager
    async def connect(self, number: int) -> AsyncGenerator[dst_server.Client]:
        async with dst_server.Client(await self._host.connect(number)) as client:
            yield client

    async def history_page(
        self,
        selection: dst_server.HistorySelection,
        *,
        page_size: int = 500,
        cursor: str | None = None,
        checkpoint: dst_server.HistoryCheckpoint | None = None,
    ) -> dst_server.HistoryPage:
        """Read one page from the journal configured on this Host."""
        raw = await self._host.history_page({
            "selection": selection.to_native(),
            "cursor": cursor,
            "page_size": page_size,
            "checkpoint": {"scope": checkpoint.scope, "cursor": checkpoint.cursor}
            if checkpoint
            else None,
        })
        return dst_server.HistoryPage.from_native(raw)

    async def journal(
        self, numbers: Sequence[int], request: JournalQuery | None = None
    ) -> JournalResult:
        result = await self._host.journal(
            numbers, (request or JournalQuery()).to_native()
        )
        return _journal_result(result)

    @asynccontextmanager
    async def follow_journal(
        self, numbers: Sequence[int], request: JournalQuery | None = None
    ) -> AsyncGenerator[JournalStream]:
        async with JournalStream(
            await self._host.follow_journal(
                numbers,
                (request or JournalQuery()).to_native(),
            )
        ) as stream:
            yield stream

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *_: object) -> None:
        pass
