from __future__ import annotations

from asyncio import Semaphore, TaskGroup
from collections.abc import Iterable
from http import HTTPMethod
from itertools import chain, product
from types import TracebackType
from typing import Self

from aiohttp import ClientError, ClientSession, ClientTimeout
from logbook import Logger
from pydantic import SecretStr

from .enums import Platform, Region
from .lobby import Capabilities, DataResponse, Lobby, Room
from .version import Builds, Version, VersionPage

logger = Logger(__name__)


class KleiClient:
    def __init__(
        self,
        access_token: SecretStr | str | None = None,
        *,
        build_url: str = "https://s3.amazonaws.com/dstbuilds/builds.json",
        version_url: str = "https://forums.kleientertainment.com/game-updates/dst/",
        region_url: str = "https://lobby-v2-cdn.klei.com/regioncapabilities-v2.json",
        lobby_url: str = "https://lobby-v2-cdn.klei.com/{region}-{platform}.json.gz",
        room_url: str = "https://lobby-v2-{region}.klei.com/lobby/read",
        lobby_concurrency: int = 8,
        room_concurrency: int = 24,
        timeout: ClientTimeout | None = None,
        session: ClientSession | None = None,
    ) -> None:
        self.access_token = (
            access_token
            if isinstance(access_token, SecretStr) or access_token is None
            else SecretStr(access_token)
        )
        self.build_url = build_url
        self.version_url = version_url
        self.region_url = region_url
        self.lobby_url = lobby_url
        self.room_url = room_url
        self.lobby_concurrency = positive("lobby_concurrency", lobby_concurrency)
        self.room_concurrency = positive("room_concurrency", room_concurrency)
        self.timeout = timeout or ClientTimeout(total=30, connect=10)
        self.session = session
        self.owns_session = session is None

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        exc_tb: TracebackType | None,
    ) -> None:
        del exc_type, exc, exc_tb
        await self.close()

    async def close(self) -> None:
        if self.owns_session and self.session is not None:
            await self.session.close()

    async def get_latest_build(self, version_type: str = "release") -> int:
        versions = Builds.model_validate_json(
            await self.request(HTTPMethod.GET, self.build_url)
        ).root.get(version_type)
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
        body = await self.request(HTTPMethod.GET, self.version_url)
        page = VersionPage.model_validate(body.decode())
        logger.info(
            "Klei version page loaded: {page}/{page_count} ({count} rows)",
            page=page.page,
            page_count=page.page_count,
            count=len(page.versions),
        )
        return page

    async def get_regions(self) -> tuple[str, ...]:
        data = Capabilities.model_validate_json(
            await self.request(HTTPMethod.GET, self.region_url)
        )
        return tuple(region.region for region in data.lobby_regions)

    async def get_lobbies(
        self,
        regions: Iterable[Region] = Region,
        platforms: Iterable[Platform] = Platform,
    ) -> tuple[Lobby, ...]:
        pairs = tuple(product(regions, platforms))
        semaphore = Semaphore(self.lobby_concurrency)

        async def load(region: Region, platform: Platform) -> tuple[Lobby, ...]:
            async with semaphore:
                return await self.lobby(region, platform)

        async with TaskGroup() as group:
            tasks = [
                group.create_task(load(region, platform)) for region, platform in pairs
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
        url = self.lobby_url.format(region=region, platform=platform.lobby_name)
        try:
            body = await self.request(HTTPMethod.GET, url)
        except ClientError as error:
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
                body,
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
        url = self.room_url.format(region=region)
        payload = {
            "__gameId": "DontStarveTogether",
            "__token": access_token.get_secret_value(),
            "query": {"__rowId": row_id},
        }
        try:
            body = await self.request(HTTPMethod.POST, url, json=payload)
        except ClientError:
            return None
        data = DataResponse[Room].model_validate_json(
            body,
            context={"region": region},
        )
        return data.rows[0] if data.rows else None

    async def request(
        self,
        method: HTTPMethod,
        url: str,
        *,
        json: object | None = None,
    ) -> bytes:
        if self.session is None:
            self.session = ClientSession(timeout=self.timeout)
        async with self.session.request(method, url, json=json) as response:
            response.raise_for_status()
            return await response.read()


def positive(name: str, value: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        msg = f"{name} must be a positive integer"
        raise ValueError(msg)
    return value


__all__ = ["KleiClient"]
