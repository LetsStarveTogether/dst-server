import asyncio
import gzip
import json
from collections.abc import Iterator
from datetime import date

import httpx2
import pytest
from pydantic import ValidationError

from dst_server.klei import (
    DataResponse,
    KleiClient,
    Platform,
    Region,
    Role,
    Room,
    VersionPage,
    VersionType,
)

BUILD_URL = "https://s3.amazonaws.com/dstbuilds/builds.json"
VERSION_URL = "https://kleiforums.com/game-updates/dst/"
REGION_URL = "https://lobby-v2-cdn.klei.com/regioncapabilities-v2.json"
LOBBY_URL = "https://lobby-v2-cdn.klei.com/us-east-1-Steam.json.gz"
ROOM_URL = "https://lobby-v2-us-east-1.klei.com/lobby/read"

VERSION_HTML = """
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
    page = VersionPage.model_validate(VERSION_HTML)

    assert page.title == "Don't Starve Together"
    assert page.page_count == 35
    assert page.followers == 262
    assert page.versions[0].number == 736959
    assert page.versions[0].type is VersionType.RELEASE
    assert page.versions[0].date == date(2026, 6, 11)
    assert page.versions[0].is_hotfix is True

    with pytest.raises(ValidationError, match="missing required nodes"):
        VersionPage.model_validate('<li class="cCmsRecord_row">broken</li>')


@pytest.mark.parametrize(
    "mods_info",
    [
        [["workshop-1", {"name": "Example mod"}]],
        ["workshop-1", "Example mod", True, None],
    ],
)
async def test_klei_client_queries_lobby_and_room_in_order(
    mods_info: object,
) -> None:
    room = lobby_row() | {
        "tick": 12345,
        "clientmodsoff": False,
        "nat": 1,
        "mods_info": mods_info,
        "players": """{
            {
                name = "Wilson",
                kuid = "KU_WILSON",
                role = "wilson",
                steam_id = 76561198000000001,
                ip = "127.0.0.1",
            },
            {
                name = "Mod Hero",
                kuid = "KU_MOD",
                role = "workshop-123-character",
            },
        }""",
    }
    calls: list[tuple[str, str, object | None]] = []
    credential = "credential-value"

    def handler(request: httpx2.Request) -> httpx2.Response:
        payload = json.loads(request.content) if request.content else None
        calls.append((request.method, str(request.url), payload))
        if str(request.url) == LOBBY_URL:
            content = gzip.compress(json.dumps({"GET": [lobby_row()]}).encode())
            return httpx2.Response(
                200,
                content=content,
                headers={"Content-Encoding": "gzip"},
            )
        if str(request.url) == ROOM_URL:
            return httpx2.Response(200, json={"GET": [room]})
        msg = f"unexpected URL: {request.url}"
        raise AssertionError(msg)

    async with httpx2.AsyncClient(transport=httpx2.MockTransport(handler)) as http:
        client = KleiClient(access_token=credential, client=http)
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
    assert rooms[0].mods_info == mods_info
    assert rooms[0].players[0].role is Role.WILSON
    assert rooms[0].players[0].steam_id == 76561198000000001
    assert rooms[0].players[1].role == "workshop-123-character"
    assert calls == [
        ("GET", LOBBY_URL, None),
        (
            "POST",
            ROOM_URL,
            {
                "__gameId": "DontStarveTogether",
                "__token": credential,
                "query": {"__rowId": "row-1"},
            },
        ),
    ]


@pytest.mark.parametrize("players", [None, "", "  \n", "{}"])
def test_klei_room_empty_players(players: str | None) -> None:
    payload = lobby_row() | {
        "tick": 1,
        "clientmodsoff": False,
        "nat": 1,
        "players": players,
    }

    response = DataResponse[Room].model_validate_json(
        json.dumps({"GET": [payload]}),
        context={"region": Region.US_EAST},
    )

    assert response.rows[0].players == ()


async def test_klei_client_parses_strict_endpoints() -> None:
    urls: list[str] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        urls.append(str(request.url))
        responses = {
            BUILD_URL: httpx2.Response(200, json={"release": [736958, "736959"]}),
            VERSION_URL: httpx2.Response(200, text=VERSION_HTML),
            REGION_URL: httpx2.Response(
                200,
                json={
                    "LobbyRegions": [
                        {"Region": "us-east-1"},
                        {"Region": "eu-central-1"},
                    ]
                },
            ),
        }
        return responses[str(request.url)]

    async with httpx2.AsyncClient(transport=httpx2.MockTransport(handler)) as http:
        client = KleiClient(client=http)
        assert await client.get_latest_build() == 736959
        assert await client.get_regions() == ("us-east-1", "eu-central-1")
        assert (await client.get_versions())[0].number == 736959
    assert urls == [BUILD_URL, REGION_URL, VERSION_URL]


@pytest.mark.parametrize("failure", ["status", "connect"])
async def test_klei_client_has_explicit_error_boundaries(failure: str) -> None:
    def handler(request: httpx2.Request) -> httpx2.Response:
        if failure == "connect":
            message = "unavailable"
            raise httpx2.ConnectError(message, request=request)
        return httpx2.Response(503)

    async with httpx2.AsyncClient(transport=httpx2.MockTransport(handler)) as http:
        credential = "credential"
        client = KleiClient(access_token=credential, client=http)
        assert await client.lobby(Region.US_EAST, Platform.STEAM) == ()
        assert await client.room("row-1", Region.US_EAST) is None
        error = httpx2.ConnectError if failure == "connect" else httpx2.HTTPStatusError
        with pytest.raises(error):
            await client.get_latest_build()


async def test_klei_client_only_closes_its_own_http_client() -> None:
    transport = httpx2.MockTransport(
        lambda _: httpx2.Response(200, json={"release": [736959]})
    )
    external = httpx2.AsyncClient(transport=transport)
    client = KleiClient(client=external)

    async with client:
        assert await client.get_latest_build() == 736959
    await client.aclose()
    assert external.is_closed is False
    await external.aclose()

    owned = KleiClient()
    async with owned:
        internal = owned._client
    assert internal.is_closed is True


async def test_room_never_redirects_its_access_token() -> None:
    requests: list[httpx2.Request] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        requests.append(request)
        return httpx2.Response(
            307,
            headers={"Location": "https://untrusted.invalid/lobby/read"},
        )

    async with httpx2.AsyncClient(
        follow_redirects=True,
        transport=httpx2.MockTransport(handler),
    ) as http:
        credential = "credential"
        client = KleiClient(access_token=credential, client=http)
        assert await client.room("row-1", Region.US_EAST) is None

    assert [str(request.url) for request in requests] == [ROOM_URL]


async def test_room_queries_require_access_token() -> None:
    client = KleiClient()

    with pytest.raises(ValueError, match="access token"):
        await client.get_rooms(())
    await client.aclose()


@pytest.mark.parametrize("value", [0, False])
def test_klei_client_rejects_invalid_concurrency(value: int) -> None:
    with pytest.raises(ValueError, match="positive integer"):
        KleiClient(lobby_concurrency=value)


@pytest.mark.parametrize(
    "region", ["us-east-1.klei.com.attacker.invalid/path", "moon-base-1"]
)
@pytest.mark.parametrize("endpoint", ["lobby", "room"])
async def test_region_is_validated_before_any_request(
    region: str,
    endpoint: str,
) -> None:
    requests: list[httpx2.Request] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        requests.append(request)
        return httpx2.Response(503)

    async with httpx2.AsyncClient(transport=httpx2.MockTransport(handler)) as http:
        credential = "local-audit-token"
        client = KleiClient(access_token=credential, client=http)
        request = (
            client.room("row-1", region)
            if endpoint == "room"
            else client.lobby(region, Platform.STEAM)
        )
        with pytest.raises(ValueError, match="Region"):
            await request

    assert requests == []


async def test_valid_region_string_is_normalized_for_request_and_response() -> None:
    def handler(request: httpx2.Request) -> httpx2.Response:
        assert str(request.url) == LOBBY_URL
        return httpx2.Response(200, json={"GET": [lobby_row()]})

    async with httpx2.AsyncClient(transport=httpx2.MockTransport(handler)) as http:
        rows = await KleiClient(client=http).lobby("us-east-1", Platform.STEAM)

    assert rows[0].region is Region.US_EAST


@pytest.mark.parametrize("finish", ["complete", "cancel", "error"])
async def test_room_queries_bound_pending_work_and_preserve_order(finish: str) -> None:
    consumed = 0
    active = 0
    ready, release, last = asyncio.Event(), asyncio.Event(), asyncio.Event()

    def rooms() -> Iterator[tuple[str, Region]]:
        nonlocal consumed
        for index in range(200):
            consumed += 1
            yield str(index), Region.US_EAST

    async def handler(request: httpx2.Request) -> httpx2.Response:
        nonlocal active
        active += 1
        if active == 3:
            ready.set()
        try:
            await release.wait()
            row_id = json.loads(request.content)["query"]["__rowId"]
            if row_id == "0":
                await last.wait()
            elif row_id == "199":
                last.set()
            await asyncio.sleep(0)
            return httpx2.Response(
                200,
                json={
                    "GET": [
                        {
                            **lobby_row(),
                            "__rowId": row_id,
                            "tick": "invalid" if finish == "error" else int(row_id),
                            "clientmodsoff": False,
                            "nat": 1,
                        }
                    ]
                },
            )
        finally:
            active -= 1

    async with httpx2.AsyncClient(transport=httpx2.MockTransport(handler)) as http:
        credential = "test"
        client = KleiClient(access_token=credential, client=http, room_concurrency=3)
        task = asyncio.create_task(client.get_rooms(rooms()))
        try:
            async with asyncio.timeout(1):
                await ready.wait()
            assert consumed == 3
            if finish == "cancel":
                task.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await task
            else:
                release.set()
                if finish == "error":
                    with pytest.raises(ExceptionGroup) as raised:
                        await task
                    assert all(
                        isinstance(error, ValidationError)
                        for error in raised.value.exceptions
                    )
                else:
                    result = await task
                    assert [room.row_id for room in result] == [
                        str(index) for index in range(200)
                    ]
                    assert consumed == 200
            assert active == 0
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
