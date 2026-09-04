import asyncio
from typing import cast
from unittest.mock import AsyncMock, Mock

import pytest

from dst_server.events import server as server_events
from dst_server.game.rpc import MAX_RESULT_LINE_BYTES
from dst_server.runtime.console import (
    MAX_RESULT_LINES,
    Console,
    IndeterminateCommandError,
    ResponseTooLargeError,
    StaleGenerationError,
    track_request,
)
from dst_server.runtime.lifecycle import Lifecycle
from dst_server.telemetry.recorder import Recorder
from dst_server.telemetry.stream import EventStream
from tests.helpers import StubWriter, feed_frame, next_frame

COMMAND_DONE = b"DST_RemoteCommandDone"


def make_console() -> tuple[Console, StubWriter, asyncio.StreamReader]:
    reader = asyncio.StreamReader()
    writer = StubWriter()
    console = Console(
        cast("asyncio.StreamWriter", writer),
        reader,
        EventStream(Recorder("test", "test")),
    )
    return console, writer, reader


async def start_command(
    console: Console,
    writer: StubWriter,
    command: str,
) -> tuple[asyncio.Task[str], bytes, bytes, bytes]:
    task = asyncio.create_task(console.execute(command))
    start, end, wrapped = await next_frame(writer)
    return task, start, end, wrapped


async def test_response_line_at_stream_limit_succeeds() -> None:
    console, writer, reader = make_console()
    task, start, end, _ = await start_command(console, writer, "first")
    result = b"x" * MAX_RESULT_LINE_BYTES
    feed_frame(reader, start, end, result)

    assert await task == result.decode()


async def test_encoded_command_line_cannot_exceed_protocol_limit() -> None:
    console, writer, _ = make_console()

    with pytest.raises(ValueError, match="command exceeds 64 KiB"):
        await console.execute("x" * MAX_RESULT_LINE_BYTES)

    assert writer.commands == []
    assert console.broken is False


@pytest.mark.parametrize(
    "lines",
    [
        (b"x" * (MAX_RESULT_LINE_BYTES // 2 + 1),) * 2,
        (b"",) * (MAX_RESULT_LINES + 1),
    ],
    ids=["total-bytes", "line-count"],
)
async def test_aggregate_response_limit_is_drained(
    lines: tuple[bytes, ...],
) -> None:
    console, writer, reader = make_console()
    first, start, end, _ = await start_command(console, writer, "first")
    feed_frame(reader, start, end, *lines)

    with pytest.raises(ResponseTooLargeError, match="exceeds 64 KiB"):
        await first

    second, start, end, _ = await start_command(console, writer, "second")
    feed_frame(reader, start, end, b"result")
    assert await second == "result"
    assert console.broken is False


async def test_framed_control_text_does_not_end_or_replay_command() -> None:
    console, writer, reader = make_console()
    task, start, end, _ = await start_command(console, writer, "mutation()")
    wrong_end = b"DST_SERVER_FRAME|00000000000000000000000000|END"
    feed_frame(reader, start, end, COMMAND_DONE, b"DST_LuaBusy", wrong_end)

    assert await task == f"{COMMAND_DONE.decode()}\nDST_LuaBusy\n{wrong_end.decode()}"
    assert len(writer.commands) == 1


async def test_result_after_frame_end_cannot_override_framed_result() -> None:
    console, writer, reader = make_console()
    task, start, end, _ = await start_command(console, writer, "first")
    framed = b'DST_SERVER_RESULT|{"ok":true,"data":1}'
    outside = b'DST_SERVER_RESULT|{"ok":true,"data":999}'
    reader.feed_data(b"\n".join((start, framed, end, outside, COMMAND_DONE, b"")))

    assert await task == framed.decode()


async def test_lua_busy_before_frame_retries_with_a_new_token() -> None:
    console, writer, reader = make_console()
    task, first_start, _, _ = await start_command(console, writer, "first")
    reader.feed_data(b"DST_LuaBusy\n")
    second_start, second_end, _ = await next_frame(writer)
    feed_frame(reader, second_start, second_end, b"result")

    assert await task == "result"
    assert second_start != first_start
    assert len(writer.commands) == 2


async def test_timeout_waiting_for_lock_keeps_console_usable() -> None:
    console, writer, reader = make_console()
    await console.lock.acquire()
    try:
        with pytest.raises(TimeoutError):
            await console.execute("blocked", completion_timeout=0.01)
    finally:
        console.lock.release()

    assert console.broken is False
    assert writer.commands == []

    task, start, end, _ = await start_command(console, writer, "next")
    feed_frame(reader, start, end, b"result")
    assert await task == "result"


async def test_timeout_after_lua_busy_keeps_console_usable() -> None:
    console, writer, reader = make_console()
    first = asyncio.create_task(console.execute("busy", completion_timeout=0.01))
    await next_frame(writer)
    reader.feed_data(b"DST_LuaBusy\n")

    with pytest.raises(TimeoutError):
        await first
    assert console.broken is False
    assert console.pending_result is None

    second, start, end, _ = await start_command(console, writer, "next")
    feed_frame(reader, start, end, b"result")
    assert await second == "result"


async def test_timeout_after_write_cancels_reader_and_breaks_console() -> None:
    console, writer, _ = make_console()
    task = asyncio.create_task(console.execute("mutation()", completion_timeout=0.01))
    await next_frame(writer)

    with pytest.raises(TimeoutError):
        await task
    assert console.broken is True
    assert console.pending_result is None

    with pytest.raises(RuntimeError, match="console is unusable"):
        await console.execute("mutation()")
    assert len(writer.commands) == 1


async def test_outer_deadline_after_write_breaks_console() -> None:
    console, writer, _ = make_console()
    loop = asyncio.get_running_loop()
    completion_deadline = loop.time() + 0.01

    with pytest.raises(TimeoutError):
        async with asyncio.timeout_at(completion_deadline):
            await console.execute(
                "mutation()",
                completion_timeout=1,
                completion_deadline=completion_deadline,
            )

    assert len(writer.commands) == 1
    assert console.broken is True
    assert console.pending_result is None


async def test_stale_generation_is_not_written_after_waiting_for_lock() -> None:
    console, writer, _ = make_console()
    current = True
    await console.lock.acquire()
    task = asyncio.create_task(console.execute("mutation()", lambda: current))

    current = False
    console.lock.release()

    with pytest.raises(StaleGenerationError, match="before the command was written"):
        await task
    assert writer.commands == []


async def test_generation_change_after_write_is_indeterminate() -> None:
    console, writer, reader = make_console()
    current = True
    task = asyncio.create_task(console.execute("mutation()", lambda: current))
    start, end, _ = await next_frame(writer)

    current = False
    feed_frame(reader, start, end, b"result")

    with pytest.raises(IndeterminateCommandError, match="result is indeterminate"):
        await task
    assert len(writer.commands) == 1


async def test_generation_change_after_lua_busy_does_not_replay(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    console, writer, reader = make_console()
    current = True
    retry_started = asyncio.Event()
    release_retry = asyncio.Event()

    async def retry_delay(delay: float) -> None:
        assert delay > 0
        retry_started.set()
        await release_retry.wait()

    monkeypatch.setattr("dst_server.runtime.console.asyncio.sleep", retry_delay)
    task = asyncio.create_task(console.execute("mutation()", lambda: current))
    await next_frame(writer)
    reader.feed_data(b"DST_LuaBusy\n")
    await retry_started.wait()
    current = False
    release_retry.set()

    with pytest.raises(StaleGenerationError, match="before the command was written"):
        await asyncio.wait_for(task, 1)
    assert len(writer.commands) == 1


async def test_generation_change_while_draining_does_not_write(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    console, writer, reader = make_console()
    first, start, end, _ = await start_command(console, writer, "first")
    first.cancel()
    with pytest.raises(asyncio.CancelledError):
        await first

    draining = asyncio.Event()
    drain_result = console.drain_result

    async def observe_drain() -> None:
        draining.set()
        await drain_result()

    monkeypatch.setattr(console, "drain_result", observe_drain)
    current = True
    second = asyncio.create_task(console.execute("mutation()", lambda: current))
    await draining.wait()
    current = False
    feed_frame(reader, start, end, b"old")

    with pytest.raises(StaleGenerationError, match="before the command was written"):
        await second
    assert len(writer.commands) == 1


async def test_cancelled_command_retrieves_later_lua_busy() -> None:
    console, writer, reader = make_console()
    loop = asyncio.get_running_loop()
    old_handler = loop.get_exception_handler()
    contexts: list[dict[str, object]] = []
    loop.set_exception_handler(lambda _, context: contexts.append(context))
    first, _, _, _ = await start_command(console, writer, "first")
    try:
        first.cancel()
        with pytest.raises(asyncio.CancelledError):
            await first

        result_task = console.pending_result
        assert result_task is not None
        reader.feed_data(b"DST_LuaBusy\n")
        await asyncio.wait((result_task,))

        second, start, end, _ = await start_command(console, writer, "second")
        feed_frame(reader, start, end, b"result")
        assert await second == "result"
        assert contexts == []
    finally:
        loop.set_exception_handler(old_handler)


async def test_cancelled_save_late_busy_releases_confirmation_barrier() -> None:
    console, writer, reader = make_console()
    lifecycle = Lifecycle()

    async def save(completed: asyncio.Event | None = None) -> server_events.SavedEvent:
        async def request() -> None:
            await console.execute("save()")
            if completed is not None:
                completed.set()

        with track_request() as state:
            return await lifecycle.wait_for_save(request, 1, state)

    first = asyncio.create_task(save())
    await next_frame(writer)
    first.cancel()
    with pytest.raises(asyncio.CancelledError):
        await first

    second_started = asyncio.Event()
    request_completed = asyncio.Event()

    async def save_again() -> server_events.SavedEvent:
        second_started.set()
        return await save(request_completed)

    second = asyncio.create_task(save_again())
    await second_started.wait()
    assert len(writer.commands) == 1

    reader.feed_data(b"DST_LuaBusy\n")
    start, end, _ = await next_frame(writer)
    feed_frame(reader, start, end, b"ok")
    await request_completed.wait()

    assert not second.done()
    expected = server_events.SavedEvent(path="session/REQUEST/2", snapshot=2)
    lifecycle.handle(expected, lambda _: None)
    assert await second == expected


async def test_oversized_response_is_drained_before_next_command() -> None:
    console, writer, reader = make_console()
    first, start, end, _ = await start_command(console, writer, "first")
    feed_frame(
        reader,
        start,
        end,
        b"x" * (MAX_RESULT_LINE_BYTES + 1) + b"y" * (MAX_RESULT_LINE_BYTES + 1),
    )

    with pytest.raises(ResponseTooLargeError, match="DST result exceeds 64 KiB"):
        await asyncio.wait_for(first, 1)

    second, start, end, _ = await start_command(console, writer, "second")
    feed_frame(reader, start, end, b"second-result")
    assert await second == "second-result"
    assert len(writer.commands) == 2
    assert console.broken is False


async def test_oversized_response_ignores_lua_busy_until_frame_end() -> None:
    console, writer, reader = make_console()
    task, start, end, _ = await start_command(console, writer, "first")
    feed_frame(
        reader,
        start,
        end,
        b"x" * (MAX_RESULT_LINE_BYTES + 1),
        b"DST_LuaBusy",
    )

    with pytest.raises(ResponseTooLargeError):
        await task
    assert len(writer.commands) == 1


async def test_cancelled_oversized_response_does_not_fail_next_command() -> None:
    console, writer, reader = make_console()
    first, start, end, _ = await start_command(console, writer, "first")
    first.cancel()
    with pytest.raises(asyncio.CancelledError):
        await first

    feed_frame(reader, start, end, b"x" * (MAX_RESULT_LINE_BYTES + 1))
    second, second_start, second_end, _ = await start_command(console, writer, "second")
    feed_frame(reader, second_start, second_end, b"second-result")

    assert await second == "second-result"
    assert len(writer.commands) == 2


async def test_cancelled_result_reader_breaks_console_before_next_write(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    console, writer, _ = make_console()

    async def cancel_result() -> None:
        assert console.pending_result is not None
        console.pending_result.cancel()
        await asyncio.gather(console.pending_result, return_exceptions=True)

    monkeypatch.setattr(writer, "drain", cancel_result)
    with pytest.raises(asyncio.CancelledError):
        await console.execute("first")

    with pytest.raises(RuntimeError, match="console is unusable"):
        await console.execute("second")
    assert len(writer.commands) == 1


async def test_incomplete_response_breaks_console_before_next_write() -> None:
    console, writer, reader = make_console()
    first, _, _, _ = await start_command(console, writer, "first")
    reader.feed_data(b"partial\n")
    reader.feed_eof()

    with pytest.raises(EOFError, match="response completed"):
        await first

    with pytest.raises(RuntimeError, match="console is unusable"):
        await console.execute("second")
    assert len(writer.commands) == 1


async def test_result_transport_failure_breaks_console() -> None:
    console, writer, reader = make_console()
    reader.set_exception(ConnectionResetError("reset"))

    with pytest.raises(ConnectionResetError, match="reset"):
        await console.execute("first")

    with pytest.raises(RuntimeError, match="console is unusable"):
        await console.execute("second")
    assert len(writer.commands) == 1


@pytest.mark.parametrize("stage", ["write", "drain"])
async def test_command_transport_failure_breaks_console(
    monkeypatch: pytest.MonkeyPatch,
    stage: str,
) -> None:
    console, writer, _ = make_console()
    failure = ConnectionResetError(f"{stage} failed")
    if stage == "write":
        monkeypatch.setattr(writer, "write", Mock(side_effect=failure))
    else:
        monkeypatch.setattr(writer, "drain", AsyncMock(side_effect=failure))

    with pytest.raises(ConnectionResetError, match="failed"):
        await console.execute("first")
    with pytest.raises(RuntimeError, match="console is unusable"):
        await console.execute("second")

    assert len(writer.commands) == (stage == "drain")
    assert console.pending_result is None


@pytest.mark.parametrize(
    ("command", "expected"),
    [
        ("not valid lua !", "unexpected symbol"),
        ('error("DST_RemoteCommandDone",0)', "DST_RemoteCommandDone"),
    ],
)
async def test_wrapper_frames_lua_errors(
    command: str,
    expected: str,
    luajit: str,
) -> None:
    console, writer, reader = make_console()
    task, start, end, wrapped = await start_command(console, writer, command)
    process = await asyncio.create_subprocess_exec(
        luajit,
        "-e",
        wrapped.decode(),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, _ = await asyncio.wait_for(process.communicate(), 5)
    reader.feed_data(stdout + COMMAND_DONE + b"\n")

    assert process.returncode == 0
    assert stdout.startswith(start + b"\n")
    assert stdout.endswith(end + b"\n")
    assert expected in await task
