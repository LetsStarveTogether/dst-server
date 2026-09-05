import asyncio
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from time import time_ns

from ulid import ULID

from dst_server.game.rpc import MAX_RESULT_LINE_BYTES, lua_string
from dst_server.game.validation import positive_timeout
from dst_server.telemetry.stream import EventStream

from .lifecycle import RequestState

COMMAND_DONE = "DST_RemoteCommandDone"
LUA_BUSY = "DST_LuaBusy"
LUA_BUSY_RETRY_DELAY = 0.1
FRAME_PREFIX = "DST_SERVER_FRAME"
DEFAULT_COMMAND_TIMEOUT = 30.0
MAX_RESULT_LINES = 1024
_request_state = ContextVar[RequestState | None](
    "dst_server_request_state", default=None
)


@contextmanager
def track_request(state: RequestState | None = None) -> Iterator[RequestState]:
    state = state or RequestState()
    token = _request_state.set(state)
    try:
        yield state
    finally:
        _request_state.reset(token)


class LuaBusyError(Exception):
    pass


class ResponseTooLargeError(RuntimeError):
    pass


class StaleGenerationError(RuntimeError):
    pass


class IndeterminateCommandError(RuntimeError):
    pass


class Console:
    def __init__(
        self,
        writer: asyncio.StreamWriter,
        reader: asyncio.StreamReader,
        game_events: EventStream,
    ) -> None:
        self.writer = writer
        self.reader = reader
        self.game_events = game_events
        self.lock = asyncio.Lock()
        self.pending_result: asyncio.Task[str] | None = None
        self.broken = False

    async def execute(
        self,
        command: str,
        generation_is_current: Callable[[], bool] | None = None,
        completion_timeout: float = DEFAULT_COMMAND_TIMEOUT,
        *,
        completion_deadline: float | None = None,
    ) -> str:
        if "\n" in command or "\r" in command:
            msg = "DST console commands must be a single line"
            raise ValueError(msg)
        timeout = positive_timeout(completion_timeout)
        command_state = RequestState()
        if completion_deadline is not None:
            try:
                return await self._execute(
                    command,
                    generation_is_current,
                    command_state,
                )
            except asyncio.CancelledError:
                if (
                    asyncio.get_running_loop().time() >= completion_deadline
                    and command_state.sent
                ):
                    await self._discard_pending_result()
                raise
        deadline = asyncio.timeout(timeout)
        try:
            async with deadline:
                return await self._execute(
                    command,
                    generation_is_current,
                    command_state,
                )
        except TimeoutError:
            if deadline.expired() and command_state.sent:
                await self._discard_pending_result()
            raise

    async def _execute(
        self,
        command: str,
        generation_is_current: Callable[[], bool] | None,
        command_state: RequestState,
    ) -> str:
        async with self.lock:
            if self.broken:
                msg = "DST console is unusable after an incomplete response"
                raise RuntimeError(msg)
            await self.drain_result()
            while True:
                if generation_is_current is not None and not generation_is_current():
                    msg = "DST generation changed before the command was written"
                    raise StaleGenerationError(msg)
                try:
                    result = await self.execute_once(command, command_state)
                except LuaBusyError:
                    await asyncio.sleep(LUA_BUSY_RETRY_DELAY)
                    continue
                if generation_is_current is not None and not generation_is_current():
                    msg = (
                        "DST generation changed while the command was executing; "
                        "the result is indeterminate"
                    )
                    raise IndeterminateCommandError(msg)
                return result

    async def execute_once(
        self,
        command: str,
        command_state: RequestState,
    ) -> str:
        token = str(ULID())
        frame_start = f"{FRAME_PREFIX}|{token}|START"
        frame_end = f"{FRAME_PREFIX}|{token}|END"
        wrapped = (
            "local p,l,c,t=print,loadstring,pcall,tostring;"
            f"p({lua_string(frame_start)});"
            f"local callback,failure=l({lua_string(command)},"
            '"@dst-server-console");'
            "local ok=false;"
            "if callback~=nil then ok,failure=c(callback) end;"
            "if not ok then local text_ok,text=c(t,failure);"
            'p(text_ok and text or "DST console command failed") end;'
            f"p({lua_string(frame_end)})"
        )
        encoded = f"{wrapped}\n".encode()
        if len(encoded) > MAX_RESULT_LINE_BYTES:
            msg = "DST console command exceeds 64 KiB"
            raise ValueError(msg)
        try:
            self.writer.write(encoded)
        except BaseException:
            self.broken = True
            raise
        command_state.mark_sent()
        state = _request_state.get()
        if state is not None:
            state.mark_sent()
        result_task = asyncio.create_task(
            self.read_result(frame_start, frame_end, command_state)
        )
        result_task.add_done_callback(self._result_finished)
        self.pending_result = result_task
        try:
            try:
                await self.writer.drain()
            except asyncio.CancelledError:
                raise
            except BaseException:
                self.broken = True
                result_task.cancel()
                await asyncio.gather(result_task, return_exceptions=True)
                raise
            await asyncio.wait((result_task,))
            return result_task.result()
        finally:
            if result_task.done():
                self._result_finished(result_task)
                self.pending_result = None

    def _result_finished(self, result_task: asyncio.Task[str]) -> None:
        if result_task.cancelled():
            self.broken = True
        else:
            result_task.exception()

    async def read_result(
        self,
        frame_start: str,
        frame_end: str,
        command_state: RequestState,
    ) -> str:
        try:
            return await self._read_result(frame_start, frame_end, command_state)
        except LuaBusyError, ResponseTooLargeError:
            raise
        except BaseException:
            self.broken = True
            raise

    async def _read_result(  # ruff: ignore[complex-structure, too-many-branches, too-many-statements]
        self,
        frame_start: str,
        frame_end: str,
        command_state: RequestState,
    ) -> str:
        lines: list[str] = []
        oversized = False
        started = False
        ended = False
        output_before_start = False
        result_bytes = 0
        result_lines = 0
        while True:
            line, line_oversized = await self._read_line()
            if line_oversized:
                if ended:
                    continue
                if started:
                    oversized = True
                    lines.clear()
                else:
                    output_before_start = True
                continue
            observed_timestamp_ns = time_ns()
            if await self.game_events.accept(
                line.rstrip(b"\r\n"), observed_timestamp_ns
            ):
                continue
            value = line.decode(errors="replace").rstrip("\r\n")
            if started and not ended and value != frame_end and not oversized:
                result_bytes += len(line.removesuffix(b"\n").removesuffix(b"\r"))
                result_lines += 1
                if (
                    result_bytes > MAX_RESULT_LINE_BYTES
                    or result_lines > MAX_RESULT_LINES
                ):
                    oversized = True
                    lines.clear()
            if ended:
                if value == COMMAND_DONE:
                    if oversized:
                        msg = "DST result exceeds 64 KiB"
                        raise ResponseTooLargeError(msg)
                    return "\n".join(lines)
                continue
            if not started:
                if value == frame_start:
                    started = True
                    continue
                if value == LUA_BUSY and not output_before_start:
                    command_state.mark_rejected()
                    state = _request_state.get()
                    if state is not None:
                        state.mark_rejected()
                    raise LuaBusyError
                if value in {COMMAND_DONE, LUA_BUSY}:
                    msg = "DST command response started with an ambiguous control line"
                    raise RuntimeError(msg)
                output_before_start = True
                continue
            if value == frame_end:
                ended = True
                continue
            if oversized:
                continue
            lines.append(value)

    async def _discard_pending_result(self) -> None:
        self.broken = True
        result_task = self.pending_result
        if result_task is None:
            return
        if not result_task.done():
            result_task.cancel()
        await asyncio.gather(result_task, return_exceptions=True)
        if self.pending_result is result_task:
            self.pending_result = None

    async def _read_line(self) -> tuple[bytes, bool]:
        oversized = False
        while True:
            try:
                return await self.reader.readuntil(b"\n"), oversized
            except asyncio.LimitOverrunError as error:
                await self.reader.readexactly(
                    min(error.consumed, MAX_RESULT_LINE_BYTES)
                )
                oversized = True
            except asyncio.IncompleteReadError as error:
                msg = "DST result stream closed before the command response completed"
                raise EOFError(msg) from error

    async def drain_result(self) -> None:
        result_task = self.pending_result
        if result_task is None:
            return
        try:
            await asyncio.wait((result_task,))
            result_task.result()
        except LuaBusyError, ResponseTooLargeError:
            pass
        finally:
            if result_task.done():
                self.pending_result = None

    async def close(self) -> None:
        self.broken = True
        self.writer.close()
        await asyncio.gather(self.writer.wait_closed(), return_exceptions=True)
