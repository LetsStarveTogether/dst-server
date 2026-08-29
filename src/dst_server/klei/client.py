from asyncio import Semaphore, TaskGroup
from collections.abc import Iterable
from itertools import chain, product
from types import TracebackType
from typing import Self

import httpx2
from logbook import Logger
from pydantic import SecretStr

from .enums import Platform, Region
from .lobby import Capabilities, DataResponse, Lobby, Room
from .version import Builds, Version, VersionPage

BUILD_URL = "https://s3.amazonaws.com/dstbuilds/builds.json"
VERSION_URL = "https://kleiforums.com/game-updates/dst/"
REGION_URL = "https://lobby-v2-cdn.klei.com/regioncapabilities-v2.json"
LOBBY_URL = "https://lobby-v2-cdn.klei.com/{region}-{platform}.json.gz"
ROOM_URL = "https://lobby-v2-{region}.klei.com/lobby/read"

logger = Logger(__name__)


class KleiClient:
    def __init__(
        self,
        access_token: SecretStr | str | None = None,
        *,
        client: httpx2.AsyncClient | None = None,
        lobby_concurrency: int = 8,
        room_concurrency: int = 24,
    ) -> None:
        self.access_token = (
            access_token
            if isinstance(access_token, SecretStr) or access_token is None
            else SecretStr(access_token)
        )
        self.lobby_concurrency = positive("lobby_concurrency", lobby_concurrency)
        self.room_concurrency = positive("room_concurrency", room_concurrency)
        self._owns_client = client is None
        self._client = (
            httpx2.AsyncClient(
                timeout=httpx2.Timeout(30, connect=10),
                transport=httpx2.AsyncHTTPTransport(
                    http2=True,
                    retries=2,
                    trust_env=False,
                ),
                trust_env=False,
            )
            if client is None
            else client
        )

    async def __aenter__(self) -> Self:
        if self._owns_client:
            await self._client.__aenter__()
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        exc_tb: TracebackType | None,
    ) -> None:
        if self._owns_client:
            await self._client.__aexit__(exc_type, exc, exc_tb)

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    async def get_latest_build(self, version_type: str = "release") -> int:
        response = await self._client.get(BUILD_URL)
        response.raise_for_status()
        versions = Builds.model_validate_json(response.content).root.get(version_type)
        if not versions:
            msg = f"Klei build response has no versions for {version_type!r}"
            raise ValueError(msg)
        latest = max(int(version) for version in versions)
        logger.info(
            "Klei latest {version_type}: {version}",
            version_type=version_type,
            version=latest,
        )
        return latest

    async def get_versions(self) -> tuple[Version, ...]:
        return (await self.get_version_page()).versions

    async def get_version_page(self) -> VersionPage:
        response = await self._client.get(VERSION_URL)
        response.raise_for_status()
        page = VersionPage.model_validate(response.text)
        logger.info(
            "Klei version page loaded: {page}/{page_count} ({count} rows)",
            page=page.page,
            page_count=page.page_count,
            count=len(page.versions),
        )
        return page

    async def get_regions(self) -> tuple[str, ...]:
        response = await self._client.get(REGION_URL)
        response.raise_for_status()
        data = Capabilities.model_validate_json(response.content)
        return tuple(region.region for region in data.lobby_regions)

    async def get_lobbies(
        self,
        regions: Iterable[Region] = Region,
        platforms: Iterable[Platform] = Platform,
    ) -> tuple[Lobby, ...]:
        semaphore = Semaphore(self.lobby_concurrency)

        async def load(region: Region, platform: Platform) -> tuple[Lobby, ...]:
            async with semaphore:
                return await self.lobby(region, platform)

        async with TaskGroup() as group:
            tasks = [
                group.create_task(load(region, platform))
                for region, platform in product(regions, platforms)
            ]
        return tuple(chain.from_iterable(task.result() for task in tasks))

    async def get_rooms(
        self,
        rooms: Iterable[tuple[str, Region]] | None = None,
    ) -> tuple[Room, ...]:
        if self.access_token is None:
            msg = "a Klei access token is required to query room details"
            raise ValueError(msg)
        if rooms is None:
            lobbies = await self.get_lobbies()
            rooms = ((lobby.row_id, lobby.region) for lobby in lobbies)
        semaphore = Semaphore(self.room_concurrency)

        async def load(row_id: str, region: Region) -> Room | None:
            async with semaphore:
                return await self.room(row_id, region)

        async with TaskGroup() as group:
            tasks = [group.create_task(load(*room)) for room in rooms]
        return tuple(room for task in tasks if (room := task.result()) is not None)

    async def lobby(
        self,
        region: Region,
        platform: Platform,
    ) -> tuple[Lobby, ...]:
        url = LOBBY_URL.format(region=region, platform=platform.lobby_name)
        try:
            response = await self._client.get(url)
            response.raise_for_status()
        except httpx2.HTTPError as error:
            logger.warning(
                "Klei lobby request failed: {region}/{platform}: {error}",
                region=region,
                platform=platform,
                error=error,
            )
            return ()
        return (
            DataResponse[Lobby]
            .model_validate_json(
                response.content,
                context={"region": region},
            )
            .rows
        )

    async def room(
        self,
        row_id: str,
        region: Region,
    ) -> Room | None:
        access_token = self.access_token
        if access_token is None:
            msg = "a Klei access token is required to query room details"
            raise ValueError(msg)
        url = ROOM_URL.format(region=region)
        payload = {
            "__gameId": "DontStarveTogether",
            "__token": access_token.get_secret_value(),
            "query": {"__rowId": row_id},
        }
        try:
            response = await self._client.post(
                url,
                json=payload,
                follow_redirects=False,
            )
            response.raise_for_status()
        except httpx2.HTTPError:
            return None
        data = DataResponse[Room].model_validate_json(
            response.content,
            context={"region": region},
        )
        return data.rows[0] if data.rows else None


def positive(name: str, value: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        msg = f"{name} must be a positive integer"
        raise ValueError(msg)
    return value
