import asyncio

import pytest

from dst_server.runtime.fds import read_line


@pytest.mark.parametrize("length", [0, 31, 32, 33, 96])
@pytest.mark.parametrize("terminated", [False, True])
async def test_protocol_lines_preserve_limits_and_discard_entire_oversized_frame(
    length: int, terminated: bool
) -> None:
    reader = asyncio.StreamReader(limit=32)
    payload = b"x" * length + (b"\nsentinel\n" if terminated else b"")
    reading = asyncio.create_task(read_line(reader))
    for offset in range(0, len(payload), 11):
        reader.feed_data(payload[offset : offset + 11])
        await asyncio.sleep(0)
    reader.feed_eof()
    line, oversized = await reading
    assert oversized is (length > 32)
    if not oversized:
        assert line == (b"x" * length + b"\n" if terminated else b"x" * length or None)
    if terminated:
        assert await read_line(reader) == (b"sentinel\n", False)
    assert await read_line(reader) == (None, False)
