"""Python bindings for the native DST room SDK."""

from os import PathLike
from typing import Any, Literal, Self

from ._native import Client as _Client
from ._native import DstError, describe, inspect
from ._native import Subscription as _Subscription
from .external import KleiClient
from .host import Host
from .logs import (
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
    "Subscription",
    "describe",
    "inspect",
]


class Client:
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
        arguments: dict[str, Any] | None = None,
        *,
        shard: str | None = None,
        timeout: float | None = None,
    ) -> Any:
        return await self._client.call(method, arguments, shard=shard, timeout=timeout)

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
