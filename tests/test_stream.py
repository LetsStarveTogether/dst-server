import asyncio
import json
from time import time_ns
from unittest.mock import Mock

import pytest
from ulid import ULID

from dst_server.telemetry import stream
from dst_server.telemetry.recorder import Recorder
from dst_server.telemetry.stream import PREFIX, QUEUE_SIZE, EventStream


def event_line(nonce: str, seq: int) -> str:
    return PREFIX + json.dumps({
        "v": 1,
        "nonce": nonce,
        "seq": seq,
        "event": "dst.world.state_changed",
        "tick": 10,
        "monotonic_ms": 20,
        "cycle": 2,
        "data": {"name": "cycles", "value": 2},
    })


async def test_full_stream_drops_newest_event_and_preserves_eof() -> None:
    recorder = Recorder("cluster", "shard")
    recorder.set_process_up(True)
    recorder.set_player_count(2)
    events = EventStream(recorder)

    for seq in range(1, QUEUE_SIZE + 2):
        assert events.accept(event_line(events.nonce, seq), seq)

    assert events.dropped == 1
    events.close()
    events.close()
    observed = []
    while event := await events.read():
        observed.append(event.record.seq)

    assert len(observed) == QUEUE_SIZE - 1
    assert observed[0] == 2
    assert observed[-1] == QUEUE_SIZE
    assert events.dropped == 2
    assert recorder.process_up is False
    assert recorder.player_count == 0
    assert await events.read() is None


async def test_close_unblocks_a_reader() -> None:
    events = EventStream(Recorder("cluster", "shard"))
    started = asyncio.Event()

    async def read() -> object:
        started.set()
        return await events.read()

    reading = asyncio.create_task(read())
    await started.wait()

    events.close()

    async with asyncio.timeout(1):
        assert await reading is None


async def test_closed_stream_ignores_late_events() -> None:
    recorder = Recorder("cluster", "shard")
    events = EventStream(recorder)
    events.close()

    assert events.accept(event_line(events.nonce, 1), 1)
    assert not events.accept("ordinary log", 1)
    assert events.invalid == 0
    assert events.dropped == 0
    assert await events.read() is None
    assert await events.read() is None


def test_rejected_events_warn_once_per_reason(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    recorder = Mock()
    event_logger = Mock()
    monkeypatch.setattr(stream, "logger", event_logger)
    events = EventStream(recorder)
    wrong_nonce = str(ULID())

    exact_limit = PREFIX + "x" * (stream.MAX_LINE_BYTES - len(PREFIX))
    for line in (
        exact_limit,
        exact_limit + "x",
        "DST_OTEL|{",
        event_line("测" * 26, 1),
        event_line(wrong_nonce, 1),
    ):
        assert events.accept(line, time_ns())
        assert events.accept(line, time_ns())

    assert [call.kwargs["reason"] for call in recorder.record_event.call_args_list] == [
        "schema",
        "schema",
        "oversized",
        "oversized",
        "schema",
        "schema",
        "schema",
        "schema",
        "nonce",
        "nonce",
    ]
    assert event_logger.warning.call_count == 3


async def test_invalid_utf8_event_is_rejected_without_poisoning_stream() -> None:
    recorder = Recorder("cluster", "shard")
    events = EventStream(recorder)
    invalid = event_line(events.nonce, 1).encode().replace(b"cycles", b"cy\xffcles")

    assert events.accept(invalid, 1)
    assert events.accept(event_line(events.nonce, 2).encode(), 2)

    observed = await events.read()
    assert observed is not None
    assert observed.record.seq == 2
    assert events.invalid == 1
