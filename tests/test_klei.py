from __future__ import annotations

import json as json_module
from datetime import date
from http import HTTPMethod
from typing import cast
from unittest.mock import AsyncMock

import pytest
from pydantic import ValidationError
from urllib3 import AsyncPoolManager
from urllib3.exceptions import HTTPError

from dst_server.klei import KleiClient, Platform, Region, VersionPage, VersionType


class StubKleiClient(KleiClient):
    def __init__(
        self,
        routes: dict[str, object],
        *,
        access_token: str,
        lobby_url: str,
        room_url: str,
    ) -> None:
        super().__init__(
            access_token=access_token,
            lobby_url=lobby_url,
            room_url=room_url,
        )
        self.routes = routes
        self.calls: list[tuple[HTTPMethod, str, object | None]] = []

    async def request(
        self,
        method: HTTPMethod,
        url: str,
        *,
        json: object | None = None,
    ) -> bytes:
        self.calls.append((method, url, json))
        return json_module.dumps(self.routes[url]).encode()


def lobby_row() -> dict[str, object]:
    return {
        "__rowId": "row-1",
        "__addr": "127.0.0.1",
        "name": "DST cluster",
        "port": 10999,
        "host": "KU_HOST",
        "connected": 3,
        "maxconnections": 6,
        "v": 736959,
        "allownewplayers": True,
        "clanonly": False,
        "clienthosted": False,
        "dedicated": True,
        "fo": False,
        "lanonly": False,
        "mods": True,
        "password": False,
        "pvp": False,
        "serverpaused": False,
        "platform": 1,
        "session": "session-id",
        "guid": "guid",
        "intent": "social",
        "steamroom": "steam-room",
        "season": "dry",
    }


def test_version_page_uses_strict_lexbor_models() -> None:
    html = """
    <h1>Don't Starve Together</h1>
    <a data-role="followButton"><span class="ipsCommentCount">262</span></a>
    <li class="cCmsRecord_row" data-rowID="2754">
      <a href="https://example.test/736959" class="cRelease"
         data-releaseID="2754" data-currentRelease>
        <span class="cUpdate_hotfix"></span>
        <h3 class="ipsType_sectionHead">
          736959 <span class="ipsBadge">Release</span>
        </h3>
        <div class="ipsDataItem_meta">Released 06/11/26</div>
      </a>
    </li>
    <ul class="ipsPagination"><li>Page 1 of 35</li></ul>
    """

    page = VersionPage.model_validate(html)

    assert page.title == "Don't Starve Together"
    assert page.page_count == 35
    assert page.followers == 262
    assert page.versions[0].number == 736959
    assert page.versions[0].type is VersionType.RELEASE
    assert page.versions[0].date == date(2026, 6, 11)
    assert page.versions[0].is_hotfix is True

    with pytest.raises(ValidationError, match="missing required nodes"):
        VersionPage.model_validate('<li class="cCmsRecord_row">broken</li>')


async def test_klei_client_queries_lobby_and_room_in_order() -> None:
    lobby_url = "https://lobby.test/us-east-1-Steam.json.gz"
    room_url = "https://room.test/us-east-1/lobby/read"
    room = lobby_row() | {"tick": 12345, "clientmodsoff": False, "nat": 1}
    credential = "credential-value"
    client = StubKleiClient(
        {
            lobby_url: {"GET": [lobby_row()]},
            room_url: {"GET": [room]},
        },
        access_token=credential,
        lobby_url="https://lobby.test/{region}-{platform}.json.gz",
        room_url="https://room.test/{region}/lobby/read",
    )

    lobbies = await client.get_lobbies(
        regions=(Region.US_EAST,),
        platforms=(Platform.STEAM,),
    )
    rooms = await client.get_rooms(((lobbies[0].row_id, Region.US_EAST),))

    assert lobbies[0].region is Region.US_EAST
    assert lobbies[0].platform is Platform.STEAM
    assert lobbies[0].season == "dry"
    assert lobbies[0].connect_code == "c_connect('127.0.0.1', 10999)"
    assert rooms[0].tick == 12345
    assert client.calls[0] == (HTTPMethod.GET, lobby_url, None)
    assert client.calls[1][0:2] == (HTTPMethod.POST, room_url)
    assert client.calls[1][2] == {
        "__gameId": "DontStarveTogether",
        "__token": credential,
        "query": {"__rowId": "row-1"},
    }


def test_klei_client_rejects_invalid_concurrency() -> None:
    with pytest.raises(ValueError, match="positive integer"):
        KleiClient(lobby_concurrency=0)


async def test_klei_client_uses_urllib3_pool() -> None:
    class Response:
        status = 200

        @property
        async def data(self) -> bytes:
            return b"response"

    response = Response()
    pool = AsyncMock(spec=AsyncPoolManager)
    pool.request.return_value = response
    client = KleiClient(pool=cast(AsyncPoolManager, pool))

    assert (
        await client.request(
            HTTPMethod.POST,
            "https://klei.test",
            json={"key": "value"},
        )
        == b"response"
    )
    pool.request.assert_awaited_once_with(
        HTTPMethod.POST,
        "https://klei.test",
        json={"key": "value"},
    )

    response.status = 503
    with pytest.raises(HTTPError, match="HTTP 503"):
        await client.request(HTTPMethod.POST, "https://klei.test")
