import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from functools import partial
from typing import Any, cast
from unittest.mock import AsyncMock, Mock

import orjson
import pytest
from pydantic import JsonValue
from ulid import ULID

from dst_server import commands as c
from dst_server.errors import IndeterminateCommandError
from dst_server.game.rpc import MAX_RESULT_LINE_BYTES, RPC_PREFIX, LuaRequestError
from dst_server.runtime.console import MAX_PENDING, Console, StaleGenerationError
from dst_server.runtime.request import RequestState, track_request
from dst_server.telemetry.recorder import Recorder
from tests.game.helpers import make_game
from tests.runtime.helpers import (
    COMMAND_DONE,
    StubWriter,
    feed_response,
    next_request,
    structured_result,
)


@asynccontextmanager
async def make_console() -> AsyncIterator[
    tuple[Console, StubWriter, asyncio.StreamReader]
]:
    writer = StubWriter()
    reader = asyncio.StreamReader()
    console = Console(
        cast("asyncio.StreamWriter", writer),
        reader,
        str(ULID()),
        Recorder("test", "Master"),
    )
    try:
        yield console, writer, reader
    finally:
        await console.close()


def packet(request: dict[str, Any], **fields: object) -> bytes:
    header = {key: request[key] for key in ("v", "nonce", "id", "generation")}
    return RPC_PREFIX + orjson.dumps(header | fields) + b"\n"


async def test_json_arguments_and_results_never_use_print_frames() -> None:
    async with make_console() as (console, writer, reader):
        arguments: dict[str, JsonValue] = {"source": 'print("hello")\nreturn "中\\""'}
        request = asyncio.create_task(console.execute("evaluate", arguments, 2))
        wire = await next_request(writer)
        assert wire["arguments"] == arguments
        assert wire["method"] == "evaluate"
        assert wire["generation"] == 2
        assert writer.commands[0].count(b"\n") == 1
        reader.feed_data(b"arbitrary console print\nDST_SERVER_RESULT|{}\n")
        feed_response(reader, wire, "result\ntext")
        assert await request == structured_result("result\ntext")


@pytest.mark.parametrize("ending", ["timeout", "cancel"])
async def test_abandoned_request_and_late_reply_cannot_poison_next_request(
    ending: str,
) -> None:
    async with make_console() as (console, writer, reader):
        first = asyncio.create_task(
            console.execute("save", {}, 0, completion_timeout=0.03)
        )
        old = await next_request(writer)
        reader.feed_data(packet(old, accepted=True))
        if ending == "cancel":
            first.cancel()
        with pytest.raises(
            asyncio.CancelledError if ending == "cancel" else TimeoutError
        ):
            await first
        assert not console.reader_task.done()
        second = asyncio.create_task(console.execute("health", {}, 0))
        current = await next_request(writer)
        feed_response(reader, current, 42)
        assert await second == structured_result(42)
        feed_response(reader, old, "late")
        third = asyncio.create_task(console.execute("health", {}, 0))
        latest = await next_request(writer)
        feed_response(reader, latest, 43)
        assert await third == structured_result(43)
        assert len(writer.commands) == 3


async def test_busy_retries_only_a_proven_unaccepted_request() -> None:
    async with make_console() as (console, writer, reader):
        state = RequestState()
        with track_request(state):
            task = asyncio.create_task(console.execute("save", {}, 0))
        first = await next_request(writer)
        assert state.sent
        reader.feed_data(b"DST_LuaBusy\n")
        second = await next_request(writer)
        assert first["id"] != second["id"]
        assert state.sent
        feed_response(reader, second, True)
        assert await task == structured_result(True)


async def test_late_busy_marks_only_its_own_request_as_rejected() -> None:
    async with make_console() as (console, writer, reader):
        state = RequestState()
        with track_request(state):
            task = asyncio.create_task(console.execute("save", {}, 0))
        await next_request(writer)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert state.sent
        reader.feed_data(b"DST_LuaBusy\n")
        await asyncio.sleep(0)
        assert not state.sent


@pytest.mark.parametrize("accepted", [False, True])
async def test_ambiguous_busy_after_timeout_is_never_replayed(accepted: bool) -> None:
    async with make_console() as (console, writer, reader):
        first = asyncio.create_task(
            console.execute("save", {}, 0, completion_timeout=0.02)
        )
        old = await next_request(writer)
        if accepted:
            reader.feed_data(packet(old, accepted=True))
        with pytest.raises(TimeoutError):
            await first
        state = RequestState()
        with track_request(state):
            second = asyncio.create_task(
                console.execute("save", {}, 0, completion_timeout=0.03)
            )
        await next_request(writer)
        reader.feed_data(b"DST_LuaBusy\nDST_RemoteCommandDone\n")
        with pytest.raises(TimeoutError):
            await second
        assert len(writer.commands) == 2
        assert state.sent


async def test_accepted_command_printing_busy_is_not_retried() -> None:
    async with make_console() as (console, writer, reader):
        task = asyncio.create_task(console.execute("save", {}, 0))
        request = await next_request(writer)
        reader.feed_data(packet(request, accepted=True) + b"DST_LuaBusy\n")
        feed_response(reader, request, True, accepted=False)
        assert await task == structured_result(True)
        assert len(writer.commands) == 1


@pytest.mark.parametrize(
    "corruption",
    ["json", "duplicate", "nonce", "id", "generation", "oversize", "missing"],
)
@pytest.mark.parametrize("method", ["health", "custom_query"])
async def test_invalid_response_fails_one_request_and_next_request_recovers(
    corruption: str, method: str
) -> None:
    async with make_console() as (console, writer, reader):
        task = asyncio.create_task(console.execute(method, {}, 0))
        request = await next_request(writer)
        reply = packet(request, result={"ok": True, "data": 1})
        if corruption == "json":
            reply = RPC_PREFIX + b"{broken\n"
        elif corruption == "duplicate":
            reply = reply.replace(b'{"v":1,', b'{"v":1,"v":1,')
        elif corruption in {"nonce", "id"}:
            reply = packet(
                request | {corruption: str(ULID())}, result={"ok": True, "data": 1}
            )
        elif corruption == "generation":
            reply = packet(request | {"generation": 1}, result={"ok": True, "data": 1})
        elif corruption == "oversize":
            reply = b"x" * (MAX_RESULT_LINE_BYTES + 1) + b"\n"
        elif corruption == "missing":
            reply = b""
        reader.feed_data(reply + COMMAND_DONE + b"\n")
        with pytest.raises(RuntimeError, match="structured result"):
            await task
        task = asyncio.create_task(console.execute("health", {}, 0))
        request = await next_request(writer)
        feed_response(reader, request, 2)
        assert await task == structured_result(2)


@pytest.mark.parametrize("code", ["stale_generation", "not_ready"])
async def test_structured_rejection_is_known_not_to_have_executed(code: str) -> None:
    async with make_console() as (console, writer, reader):
        state = RequestState()
        with track_request(state):
            task = asyncio.create_task(console.execute("save", {}, 0))
        request = await next_request(writer)
        reader.feed_data(
            packet(request | {"generation": 1}, result={"ok": False, "error": code})
            + COMMAND_DONE
            + b"\n"
        )
        with pytest.raises(StaleGenerationError):
            await task
        assert not state.sent
        assert len(writer.commands) == 1


async def test_generation_guards_cover_queued_and_executing_commands() -> None:
    async with make_console() as (console, writer, reader):
        current = True
        with pytest.raises(StaleGenerationError):
            await console.execute("health", {}, 0, lambda: False)
        assert writer.commands == []
        task = asyncio.create_task(console.execute("save", {}, 0, lambda: current))
        request = await next_request(writer)
        current = False
        feed_response(reader, request, True)
        with pytest.raises(IndeterminateCommandError):
            await task
        assert len(writer.commands) == 1


async def test_queued_timeout_does_not_write_or_cancel_current_reader() -> None:
    async with make_console() as (console, writer, reader):
        first = asyncio.create_task(console.execute("health", {}, 0))
        request = await next_request(writer)
        with pytest.raises(TimeoutError):
            await console.execute("health", {}, 0, completion_timeout=0.01)
        assert len(writer.commands) == 1
        feed_response(reader, request, True)
        assert await first == structured_result(True)


@pytest.mark.parametrize("ending", ["eof", "close"])
@pytest.mark.parametrize("method", ["save", "health"])
async def test_stream_shutdown_wakes_requests_and_reaps_reader(
    ending: str, method: str
) -> None:
    async with make_console() as (console, writer, reader):
        task = asyncio.create_task(console.execute(method, {}, 0))
        await next_request(writer)
        if ending == "eof":
            reader.feed_eof()
        else:
            await console.close()
        with pytest.raises(IndeterminateCommandError if method == "save" else EOFError):
            await task
        with pytest.raises(EOFError):
            await console.execute(method, {}, 0)
        assert console.closed
        assert console.reader_task.done()


async def test_request_size_is_bounded_before_writing() -> None:
    async with make_console() as (console, writer, _):
        with pytest.raises(ValueError, match="atomic pipe limit"):
            await console.execute(
                "evaluate", {"source": "x" * MAX_RESULT_LINE_BYTES}, 0
            )
        assert not writer.commands
        assert not console.pending


async def test_missing_native_barriers_have_bounded_memory() -> None:
    async with make_console() as (console, writer, reader):
        for _ in range(MAX_PENDING + 2):
            task = asyncio.create_task(console.execute("health", {}, 0))
            await next_request(writer)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        assert len(console.pending) == MAX_PENDING
        task = asyncio.create_task(console.execute("health", {}, 0))
        request = await next_request(writer)
        feed_response(reader, request, True)
        assert await task == structured_result(True)
        assert not console.pending


async def test_unread_input_blocks_new_writes_after_caller_timeout() -> None:
    async with make_console() as (console, writer, reader):
        with pytest.raises(TimeoutError):
            await console.execute("health", {}, 0, completion_timeout=0.01)
        assert len(writer.commands) == 1
        second = asyncio.create_task(console.execute("health", {}, 0))
        await asyncio.sleep(0.03)
        assert len(writer.commands) == 1
        await next_request(writer)  # Native consumed input, without any response.
        request = await next_request(writer)
        feed_response(reader, request, True)
        assert await second == structured_result(True)
        assert len(writer.commands) == 2


@pytest.mark.parametrize("command", [c.Save(), c.RegenerateShard(), c.Health()])
@pytest.mark.parametrize("result", ["missing", "rejected"])
async def test_native_result_certainty_reaches_game_client(
    command: c.Request[Any], result: str
) -> None:
    async with make_console() as (console, writer, reader):
        game, _ = make_game()
        game.execute_ready = partial(console.execute, generation=0)

        async def reload(
            method: str, arguments: dict[str, JsonValue], completion_timeout: float
        ) -> bytes:
            return await console.execute(
                method, arguments, 0, completion_timeout=completion_timeout
            )

        game.execute_reload = reload
        task = asyncio.create_task(game.invoke(command))
        request = await next_request(writer)
        reply = (
            packet(request, result={"ok": False, "error": "rejected"})
            if result == "rejected"
            else b""
        )
        reader.feed_data(packet(request, accepted=True) + reply + COMMAND_DONE + b"\n")
        expected = (
            LuaRequestError
            if result == "rejected"
            else IndeterminateCommandError
            if c.operation("agent", command).mutation
            else RuntimeError
        )
        with pytest.raises(expected) as caught:
            await task
        assert type(caught.value) is expected
        assert not console.pending


async def test_result_after_finished_frame_does_not_retire_the_next_request() -> None:
    async with make_console() as (console, writer, reader):
        task = asyncio.create_task(console.execute("evaluate", {}, 0))
        old = await next_request(writer)
        reader.feed_data(packet(old, accepted=True) + COMMAND_DONE + b"\n")
        with pytest.raises(IndeterminateCommandError):
            await task
        second = asyncio.create_task(console.execute("health", {}, 0))
        request = await next_request(writer)
        feed_response(reader, old, True, accepted=False, done=False)
        feed_response(reader, request, 42)
        assert await second == structured_result(42)
        assert len(writer.commands) == 2
        assert not console.pending


@pytest.mark.parametrize("stage", ["write", "drain"])
async def test_mutation_write_failure_preserves_dispatch_certainty(
    stage: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    async with make_console() as (console, writer, _):
        failure = BrokenPipeError("closed input")
        monkeypatch.setattr(
            writer,
            stage,
            Mock(side_effect=failure)
            if stage == "write"
            else AsyncMock(side_effect=failure),
        )
        state = RequestState()
        with (
            track_request(state),
            pytest.raises(
                BrokenPipeError if stage == "write" else IndeterminateCommandError
            ),
        ):
            await console.execute("save", {}, 0)
        assert state.sent is (stage == "drain")
        assert len(writer.commands) == (stage == "drain")


async def test_request_at_atomic_pipe_limit_is_sent_whole() -> None:
    async with make_console() as (console, writer, reader):
        header = {
            "v": 1,
            "nonce": console.nonce,
            "id": str(ULID()),
            "generation": 0,
            "method": "evaluate",
            "arguments": {"source": ""},
        }
        overhead = len(RPC_PREFIX + orjson.dumps(header) + b"\n")
        source = "x" * (4096 - overhead)
        task = asyncio.create_task(console.execute("evaluate", {"source": source}, 0))
        request = await next_request(writer)
        assert len(writer.commands[0]) == 4096
        assert request["arguments"]["source"] == source
        feed_response(reader, request, True)
        assert await task == structured_result(True)
        with pytest.raises(ValueError, match="atomic pipe limit"):
            await console.execute("evaluate", {"source": source + "x"}, 0)
        assert len(writer.commands) == 1
