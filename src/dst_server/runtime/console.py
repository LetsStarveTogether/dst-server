import asyncio
import fcntl
import select
import termios
from array import array
from collections.abc import Callable
from dataclasses import dataclass

import orjson
from pydantic import JsonValue
from ulid import ULID

from dst_server.commands import validate_json_structure
from dst_server.concurrency import cancel_tasks, complete
from dst_server.errors import IndeterminateCommandError
from dst_server.game.rpc import (
    MAX_RESULT_LINE_BYTES,
    RPC_PREFIX,
    RPC_RESPONSE,
    Accepted,
    Failure,
)
from dst_server.telemetry.recorder import Recorder
from dst_server.timeouts import DEFAULT_COMMAND_TIMEOUT, positive_timeout

from .fds import read_line
from .request import RequestState, current_request

COMMAND_DONE = b"DST_RemoteCommandDone"
LUA_BUSY = b"DST_LuaBusy"
LUA_BUSY_RETRY_DELAY = 0.1
# Native command input consumes read chunks, not a newline-delimited stream.
MAX_REQUEST_BYTES = select.PIPE_BUF
MAX_PENDING = 64


class LuaBusyError(Exception):
    pass


class StaleGenerationError(RuntimeError):
    pass


@dataclass(slots=True)
class Pending:
    method: str
    generation: int
    future: asyncio.Future[bytes]
    tracked: RequestState | None
    accepted: bool = False
    native_done: bool = False

    def reject(self) -> None:
        if self.tracked is not None:
            self.tracked.mark_rejected()

    def fail(self, error: Exception) -> None:
        if not self.future.done():
            self.future.set_exception(error)


class Console:
    def __init__(
        self,
        writer: asyncio.StreamWriter,
        reader: asyncio.StreamReader,
        nonce: str,
        recorder: Recorder,
    ) -> None:
        self.writer = writer
        self.reader = reader
        self.nonce = nonce
        self.recorder = recorder
        self.lock = asyncio.Lock()
        self.pending: dict[str, Pending] = {}
        self.synchronized = True
        self.closed = False
        self.reader_task = asyncio.create_task(self._read_results())

    async def execute(
        self,
        method: str,
        arguments: dict[str, JsonValue],
        generation: int,
        generation_is_current: Callable[[], bool] | None = None,
        completion_timeout: float = DEFAULT_COMMAND_TIMEOUT,
        *,
        completion_deadline: float | None = None,
    ) -> bytes:
        deadline = (
            completion_deadline
            if completion_deadline is not None
            else asyncio.get_running_loop().time()
            + positive_timeout(completion_timeout)
        )
        async with asyncio.timeout_at(deadline), self.lock:
            while True:
                if self.closed:
                    msg = "DST result stream is closed"
                    raise EOFError(msg)
                await self._wait_input_consumed()
                if generation_is_current is not None and not generation_is_current():
                    msg = "DST generation changed before the command was written"
                    raise StaleGenerationError(msg)
                try:
                    result = await self._send(method, arguments, generation)
                except LuaBusyError:
                    await asyncio.sleep(LUA_BUSY_RETRY_DELAY)
                    continue
                if generation_is_current is not None and not generation_is_current():
                    msg = "DST generation changed while the command was executing"
                    raise IndeterminateCommandError(msg)
                return result

    async def _wait_input_consumed(self) -> None:
        # Linux pipe occupancy is independent of RPC replies: even native's
        # no-Lua-context path consumes input without producing Busy or Done.
        remaining = array("i", [0])
        pipe = self.writer.get_extra_info("pipe")
        while True:
            if self.closed:
                msg = "DST result stream is closed"
                raise EOFError(msg)
            fcntl.ioctl(pipe, termios.FIONREAD, remaining, True)
            if not remaining[0] and not self.writer.transport.get_write_buffer_size():
                return
            await asyncio.sleep(0.01)

    async def _send(
        self, method: str, arguments: dict[str, JsonValue], generation: int
    ) -> bytes:
        request_id = str(ULID())
        encoded = (
            RPC_PREFIX
            + orjson.dumps({
                "v": 1,
                "nonce": self.nonce,
                "id": request_id,
                "generation": generation,
                "method": method,
                "arguments": arguments,
            })
            + b"\n"
        )
        if len(encoded) > MAX_REQUEST_BYTES:
            msg = f"DST request exceeds the {MAX_REQUEST_BYTES}-byte atomic pipe limit"
            raise ValueError(msg)
        pending = Pending(
            method,
            generation,
            asyncio.get_running_loop().create_future(),
            current_request.get(),
        )
        # Retain late replies only within a fixed window; IDs prevent any replay.
        if len(self.pending) == MAX_PENDING:
            del self.pending[next(iter(self.pending))]
            self.synchronized = False
        if any(
            item.future.cancelled() and not item.native_done
            for item in self.pending.values()
        ):
            self.synchronized = False
        self.pending[request_id] = pending
        try:
            try:
                self.writer.write(encoded)
                if pending.tracked is not None:
                    pending.tracked.mark_sent()
                await self.writer.drain()
            except asyncio.CancelledError:
                raise
            except Exception:
                self.closed = True
                raise
            return await pending.future
        finally:
            # The permanent reader survives caller cancellation and deadlines.
            # A missing native barrier makes subsequent uncorrelated Busy unsafe.
            if request_id in self.pending and not pending.future.done():
                pending.future.cancel()
            if pending.future.done() and not pending.future.cancelled():
                pending.future.exception()

    def _diagnostic(self, reason: str) -> None:
        self.recorder.record_event("invalid", reason=f"rpc_{reason}")

    async def _read_results(self) -> None:
        try:
            while True:
                line, oversized = await read_line(self.reader)
                if line is None or not line.endswith(b"\n"):
                    break
                if oversized or len(line) > MAX_RESULT_LINE_BYTES:
                    self._diagnostic("oversized")
                    continue
                self._receive(line.rstrip(b"\r\n"))
        finally:
            self.closed = True
            for pending in self.pending.values():
                pending.fail(EOFError("DST result stream is closed"))
            self.pending.clear()

    def _native_done(self, request_id: str) -> None:
        pending = self.pending[request_id]
        pending.native_done = True
        # Save completion is asynchronous and explicitly correlated by ID.
        if pending.method != "save" or not pending.accepted:
            pending.fail(RuntimeError("DST command did not return a structured result"))
        if pending.future.done():
            del self.pending[request_id]

    def _receive(self, line: bytes) -> None:  # ruff: ignore[complex-structure, too-many-branches]
        if line in {COMMAND_DONE, LUA_BUSY}:
            native = [key for key, item in self.pending.items() if not item.native_done]
            if line == COMMAND_DONE:
                if self.synchronized and native:
                    self._native_done(native[0])
            elif (
                self.synchronized
                and len(native) == 1
                and not self.pending[native[0]].accepted
            ):
                pending = self.pending.pop(native[0])
                pending.reject()
                pending.fail(LuaBusyError())
            else:
                self._diagnostic("unattributed_busy")
            return
        if not line.startswith(RPC_PREFIX):
            return
        try:
            payload = line.removeprefix(RPC_PREFIX)
            validate_json_structure(payload)
            response = RPC_RESPONSE.validate_json(payload, strict=True)
        except ValueError:
            self._diagnostic("invalid_response")
            return
        if response.nonce != self.nonce or response.id not in self.pending:
            self._diagnostic("unmatched_response")
            return
        pending = self.pending[response.id]
        rejected = (
            not isinstance(response, Accepted)
            and isinstance(response.result, Failure)
            and response.result.error
            in {"not_ready", "stale_generation", "invalid_request"}
            and not pending.accepted
        )
        if response.generation != pending.generation and not rejected:
            self._diagnostic("generation_mismatch")
            return
        # Only a reply within an unfinished native frame establishes its place
        # in the input stream. Deferred save results may arrive after later calls.
        if not pending.native_done:
            for earlier in list(self.pending):
                if earlier == response.id:
                    break
                if not self.pending[earlier].native_done:
                    self._native_done(earlier)
            self.synchronized = True
        if isinstance(response, Accepted):
            pending.accepted = True
            return
        if rejected:
            pending.reject()
            if response.result.error in {"not_ready", "stale_generation"}:
                pending.fail(
                    StaleGenerationError("DST driver is not ready for this generation")
                )
                return
        else:
            pending.accepted = True
        if pending.future.done():
            self._diagnostic("late_response")
        else:
            pending.future.set_result(response.result.model_dump_json().encode())
        if pending.native_done:
            del self.pending[response.id]

    async def close(self) -> None:
        await complete(self._close())

    async def _close(self) -> None:
        self.closed = True
        self.writer.close()
        try:
            await cancel_tasks(self.reader_task)
        finally:
            await asyncio.gather(self.writer.wait_closed(), return_exceptions=True)
