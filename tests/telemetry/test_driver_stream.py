from unittest.mock import AsyncMock, Mock

import orjson
import pytest

from dst_server.models.driver import DriverFailed, DriverReady, DriverStarting
from dst_server.runtime import Server, ServerConfig
from dst_server.telemetry.recorder import Recorder
from dst_server.telemetry.stream import EventStream


@pytest.mark.parametrize("prefix", ["", "[00:00:01]: ", "[125:59:59]: "])
async def test_driver_records_bypass_game_event_queue(prefix: str) -> None:
    observed = AsyncMock()
    stream = EventStream(Recorder("cluster", "shard"), observed)
    records = [
        {"nonce": stream.nonce, "generation": 2},
        {
            "nonce": stream.nonce,
            "health": {
                "protocol": 2,
                "generation": 2,
                "telemetry_status": "disabled",
                "last_error": None,
                "events_emitted": 0,
                "errors": 0,
            },
        },
        {"nonce": stream.nonce, "generation": 2, "error": "installation_failed"},
    ]
    for record in records:
        assert await stream.accept(
            prefix + "DST_DRIVER|" + orjson.dumps(record).decode(), 1
        )
    assert [type(call.args[0]) for call in observed.await_args_list] == [
        DriverStarting,
        DriverReady,
        DriverFailed,
    ]
    assert stream.queue.empty()
    assert stream.invalid == 0


@pytest.mark.parametrize(
    "payload",
    [
        b"\xff",
        b"{}",
        b'{"nonce": "01ARZ3NDEKTSV4RRFFQ69G5FAV", "generation":2}',
        b" " * (64 * 1024),
    ],
)
async def test_invalid_driver_records_cannot_change_readiness(payload: bytes) -> None:
    observed = AsyncMock()
    stream = EventStream(Recorder("cluster", "shard"), observed)
    assert await stream.accept(b"DST_DRIVER|" + payload, 1)
    observed.assert_not_awaited()
    assert stream.invalid == 1
    mixed = {
        "nonce": stream.nonce,
        "generation": 2,
        "health": {},
        "error": "installation_failed",
    }
    assert await stream.accept(b"DST_DRIVER|" + orjson.dumps(mixed), 2)
    observed.assert_not_awaited()
    assert stream.invalid == 2
    assert not await stream.accept(b"player said: DST_DRIVER|" + orjson.dumps(mixed), 3)


async def test_anonymous_bootstrap_failure_preserves_driver_state() -> None:
    server = Server(ServerConfig(shard="test"))
    observe = server.recorder.observe_log = Mock()
    for generation in (None, 9):
        record = {
            "nonce": None,
            "generation": generation,
            "error": "configuration_failed",
        }
        assert await server.game_events.accept(b"DST_DRIVER|" + orjson.dumps(record), 1)
        diagnostic = observe.call_args.kwargs
        assert diagnostic["body"] == {
            "kind": "driver_bootstrap_unverified",
            "reason": "configuration_failed",
        }
        assert diagnostic["severity_text"] == "ERROR"
        assert server.driver.generation == 0
        assert not server.driver.is_ready(0)
        assert server.driver_error is None
    await server.finish()
