import asyncio

import pytest

from dst_server.events import server as events
from dst_server.runtime import lifecycle as lifecycle_module
from dst_server.runtime.lifecycle import Lifecycle


async def test_bounded_native_lifecycle_never_drops_a_save_for_eof() -> None:
    lifecycle = Lifecycle()
    lifecycle.queue = asyncio.Queue(maxsize=1)
    reader = asyncio.StreamReader()
    reader.feed_data(b"DST_Saved|session/ABC/1\nDST_Saved|session/ABC/2\n")
    reader.feed_eof()
    pumping = asyncio.create_task(lifecycle.pump(reader, lambda _: None))
    try:
        await asyncio.sleep(0)
        assert not pumping.done()
        assert await lifecycle.read() == events.SavedEvent(
            path="session/ABC/1", snapshot=1
        )
        await asyncio.wait_for(pumping, 1)
        assert await lifecycle.read() == events.SavedEvent(
            path="session/ABC/2", snapshot=2
        )
        assert await lifecycle.read() is None
        assert lifecycle.save_count == 2
    finally:
        pumping.cancel()
        await asyncio.gather(pumping, return_exceptions=True)


async def test_stats_do_not_evict_lifecycle_or_change_generation() -> None:
    lifecycle = Lifecycle()
    generations: list[int] = []
    reader = asyncio.StreamReader()
    reader.feed_data(
        b"DST_SessionId|ABC\nDST_Saved|session/ABC/2\n"
        + b"DST_Stats|1|2|3|4|5\n" * 5000
    )
    reader.feed_eof()
    await lifecycle.pump(reader, generations.append)

    assert generations == [1]
    assert await lifecycle.read() == events.SessionEvent(session_id="ABC")
    assert await lifecycle.read() == events.SavedEvent(path="session/ABC/2", snapshot=2)
    assert await lifecycle.read() is None


@pytest.mark.parametrize("ending", [b"\n", b"\r\n", b""])
async def test_native_stream_recovers_after_an_oversized_message(ending: bytes) -> None:
    lifecycle = Lifecycle()
    reader = asyncio.StreamReader(limit=64)
    reader.feed_data(b"DST_Stats|" + b"9" * 500 + b"\nDST_Saved|session/ABC/3" + ending)
    reader.feed_eof()
    await lifecycle.pump(reader, lambda _: None)

    assert await lifecycle.read() == events.SavedEvent(path="session/ABC/3", snapshot=3)
    assert await lifecycle.read() is None


async def test_unknown_and_malformed_events_do_not_mutate_lifecycle_state() -> None:
    lifecycle = Lifecycle()
    reader = asyncio.StreamReader()
    reader.feed_data(
        b"DST_SessionId|\nDST_Stopping|fake\nDST_Saved|" + b"x" * 4097 + b"\n"
    )
    reader.feed_eof()
    await lifecycle.pump(reader, lambda _: pytest.fail("unexpected session"))

    assert lifecycle.session_generation == 0
    assert lifecycle.save_count == 0
    assert not lifecycle.ready
    assert not lifecycle.stopping.is_set()


async def test_lifecycle_observation_timestamp_is_captured_before_consumer_runs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    lifecycle = Lifecycle()
    now = 100
    monkeypatch.setattr(lifecycle_module, "time_ns", lambda: now)
    reader = asyncio.StreamReader()
    reader.feed_data(b"DST_Saved|session/ABC/3\n")
    reader.feed_eof()
    await lifecycle.pump(reader, lambda _: None)
    now = 200

    record = await lifecycle.read_observed()
    assert record is not None
    assert record.observed_timestamp_ns == 100
    assert record.event == events.SavedEvent(path="session/ABC/3", snapshot=3)
    assert await lifecycle.read_observed() is None
