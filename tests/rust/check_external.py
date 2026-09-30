"""Exercise the installed native Klei binding against a local HTTP service."""

import asyncio
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from dst_server import DstError
from dst_server.external import KleiClient

TOKEN = "local-binding-test-credential"  # ruff: ignore[hardcoded-password-string] - loopback fixture credential
STARTED = threading.Event()
DISCONNECTED = threading.Event()


def lobby_row() -> dict:
    return {
        "__rowId": "example",
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
        "future_field": {
            "exact": 9_007_199_254_740_993,
            "nested": [None, False, 0, "", 0.25],
        },
    }


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *_: object) -> None:
        pass

    def respond(self, value: object, status: int = 200) -> None:
        data = value.encode() if isinstance(value, str) else json.dumps(value).encode()
        self.send_response(status)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self) -> None:
        if self.path == "/builds":
            self.respond({"release": [736950, "736959"]})
        elif self.path == "/regions":
            self.respond({"LobbyRegions": [{"Region": "us-east-1"}]})
        elif self.path == "/versions":
            self.respond(
                '<li class="cCmsRecord_row" data-rowID="2754">'
                '<a href="https://example.test/736959" '
                'class="cRelease" data-currentRelease>'
                '<h3 class="ipsType_sectionHead">736959 '
                '<span class="ipsBadge">Release</span></h3>'
                '<div class="ipsDataItem_meta">Released 06/11/26</div></a></li>'
            )
        elif self.path == "/lobby/us-east-1-Steam":
            self.respond({"GET": [lobby_row()]})
        elif self.path == "/lobby/eu-central-1-Steam":
            self.respond(TOKEN, 503)
        elif self.path.startswith("/lobby/"):
            self.respond({"GET": []})
        elif self.path == "/slow":
            STARTED.set()
            if not self.rfile.read(1):
                DISCONNECTED.set()
        else:
            self.respond({}, 404)

    def do_POST(self) -> None:
        request = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        assert request["__token"] == TOKEN
        row_id = request["query"]["__rowId"]
        if row_id == "missing":
            self.respond({"GET": []})
            return
        if row_id == "failed":
            self.respond(TOKEN, 503)
            return
        row = lobby_row() | {
            "tick": 12345,
            "clientmodsoff": False,
            "nat": 1,
            "players": (
                "return {{name='Hero',netid='76561198000000002',"
                "prefab='mod_character',colour='F02D0EFF',eventlevel=1}}"
            ),
        }
        self.respond({"GET": [row]})


async def check(client: KleiClient, base: str) -> None:
    assert await client.get_regions() == ["us-east-1"]
    versions = await client.get_versions()
    assert versions[0]["number"] == 736959
    assert versions[0]["date"] == "2026-06-11"
    assert versions[0]["is_current_release"] is True
    lobby = (await client.lobby("us-east-1"))[0]
    assert lobby["future_field"] == {
        "exact": 9_007_199_254_740_993,
        "nested": [None, False, 0, "", 0.25],
    }
    assert lobby["allownewplayers"] is True
    room = await client.room("example", "us-east-1")
    assert room["players"][0]["prefab"] == "mod_character"
    assert room["players"][0]["eventlevel"] == 1
    assert await client.room("missing", "us-east-1") is None
    lobbies = await client.get_lobbies(["us-east-1", "eu-central-1"], ["Steam"])
    assert [item["result"]["status"] for item in lobbies] == ["success", "failure"]
    assert lobbies[1]["result"]["error"]["details"] == {"status": 503}
    rooms = await client.get_rooms([
        {"row_id": "example", "region": "us-east-1"},
        {"row_id": "missing", "region": "us-east-1"},
        {"row_id": "failed", "region": "us-east-1"},
    ])
    assert [item["query"]["row_id"] for item in rooms] == [
        "example",
        "missing",
        "failed",
    ]
    assert rooms[1]["result"] == {"status": "success", "value": None}
    assert rooms[2]["result"]["error"]["code"] == "transport"
    assert TOKEN not in repr(rooms)
    discovered = await client.discover_rooms()
    assert len(discovered["lobbies"]) == 20
    assert len(discovered["rooms"]) == 1
    failure = None
    try:
        await client.room("failed", "us-east-1")
    except DstError as error:
        failure = (error.code, error.details, str(error))
    assert failure is not None
    assert failure[:2] == ("transport", {"status": 503})
    assert TOKEN not in failure[2]
    slow = KleiClient(endpoints={"builds": base + "/slow"})
    operation = asyncio.create_task(slow.get_latest_build())
    async with asyncio.timeout(3):
        assert await asyncio.to_thread(STARTED.wait, 3)
        operation.cancel()
        result = await asyncio.gather(operation, return_exceptions=True)
        assert isinstance(result[0], asyncio.CancelledError)
        assert await asyncio.to_thread(DISCONNECTED.wait, 3)


def main() -> None:
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{server.server_port}"
    try:
        client = KleiClient(
            TOKEN,
            endpoints={
                "builds": base + "/builds",
                "versions": base + "/versions",
                "regions": base + "/regions",
                "lobby": base + "/lobby/{region}-{platform}",
                "room": base + "/room/{region}",
            },
        )
        assert TOKEN not in repr(client)
        assert asyncio.run(client.get_latest_build()) == 736959
        assert asyncio.run(client.get_latest_build()) == 736959
        asyncio.run(check(client, base))
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=3)
    print("Python native Klei types, batches, cancellation and loop replacement passed")  # ruff: ignore[print]


if __name__ == "__main__":
    main()
