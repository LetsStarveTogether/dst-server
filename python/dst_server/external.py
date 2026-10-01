"""Klei reads performed by the native SDK, exposed as asyncio coroutines."""

from collections.abc import Sequence
from typing import Any, Literal, TypedDict, cast

from ._native import KleiClient as _KleiClient

type Region = Literal["us-east-1", "eu-central-1", "ap-southeast-1", "ap-east-1"]
type Platform = Literal["Steam", "PSN", "Rail", "XBone", "Switch"]


class KleiEndpoints(TypedDict, total=False):
    """Explicit trusted endpoint overrides; room endpoints receive the token."""

    builds: str
    versions: str
    regions: str
    lobby: str
    room: str


class RoomQuery(TypedDict):
    row_id: str
    region: Region


class KleiClient:
    """Bounded Klei requests with per-query results for batches.

    Lobby and room results retain upstream fields and decoded players.
    Batch results contain ``query`` and ``result`` objects, including failures.
    Cancelling a coroutine cancels its pending native HTTP requests.
    """

    __slots__ = ("_client",)

    def __init__(
        self,
        access_token: str | None = None,
        *,
        endpoints: KleiEndpoints | None = None,
        request_timeout: float = 30.0,
        connect_timeout: float = 10.0,
        max_response_bytes: int = 33_554_432,
        lobby_concurrency: int = 8,
        room_concurrency: int = 24,
    ) -> None:
        self._client = _KleiClient(
            access_token,
            endpoints=cast("dict[str, str] | None", endpoints),
            request_timeout=request_timeout,
            connect_timeout=connect_timeout,
            max_response_bytes=max_response_bytes,
            lobby_concurrency=lobby_concurrency,
            room_concurrency=room_concurrency,
        )

    async def get_latest_build(self, version_type: str = "release") -> int:
        return await self._client.get_latest_build(version_type)

    async def get_versions(self) -> list[dict[str, Any]]:
        return await self._client.get_versions()

    async def get_regions(self) -> list[str]:
        return await self._client.get_regions()

    async def lobby(
        self, region: Region, platform: Platform = "Steam"
    ) -> list[dict[str, Any]]:
        return await self._client.lobby(region, platform)

    async def room(self, row_id: str, region: Region) -> dict[str, Any] | None:
        return await self._client.room(row_id, region)

    async def get_lobbies(
        self,
        regions: Sequence[Region] | None = None,
        platforms: Sequence[Platform] | None = None,
    ) -> list[dict[str, Any]]:
        return await self._client.get_lobbies(regions, platforms)

    async def get_rooms(self, rooms: Sequence[RoomQuery]) -> list[dict[str, Any]]:
        return await self._client.get_rooms([dict(room) for room in rooms])

    async def discover_rooms(self) -> dict[str, Any]:
        return await self._client.discover_rooms()
