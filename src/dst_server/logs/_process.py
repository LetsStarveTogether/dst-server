"""Owned, bounded output from the two native log readers."""

import asyncio
from asyncio import create_subprocess_exec
from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager, suppress
from typing import Self, cast

from dst_server.concurrency import complete

_DIAGNOSTIC_LIMIT = 64 * 1024
_CLOSE_TIMEOUT = 3.0


class LogProcessError(RuntimeError):
    def __init__(
        self,
        command: tuple[str, ...],
        returncode: int,
        diagnostics: str,
        diagnostics_truncated: bool,
    ) -> None:
        self.command = command
        self.returncode = returncode
        self.diagnostics = diagnostics
        self.diagnostics_truncated = diagnostics_truncated
        super().__init__(f"{command[0]} exited with status {returncode}: {diagnostics}")


def positive_bytes(value: int) -> int:
    if type(value) is not int or value <= 0:
        message = "log output byte limits must be positive integers"
        raise ValueError(message)
    return value


async def _discard(stream: asyncio.StreamReader) -> None:
    while await stream.read(65536):
        pass


class LogOutput:
    def __init__(
        self,
        process: asyncio.subprocess.Process,
        command: tuple[str, ...],
        max_record_bytes: int,
        max_output_bytes: int | None,
    ) -> None:
        self.process = process
        self.command = command
        self.stdout = cast(asyncio.StreamReader, process.stdout)
        self.stderr = cast(asyncio.StreamReader, process.stderr)
        self.max_record_bytes = max_record_bytes
        self.max_output_bytes = max_output_bytes
        self._received = 0
        self._diagnostics = bytearray()
        self.diagnostics_truncated = False
        self._stderr_task = asyncio.create_task(self._read_diagnostics())

    @property
    def diagnostics(self) -> str:
        return self._diagnostics.decode(errors="replace").strip()

    async def _read_diagnostics(self) -> None:
        while chunk := await self.stderr.read(_DIAGNOSTIC_LIMIT):
            self._diagnostics.extend(chunk)
            if len(self._diagnostics) > _DIAGNOSTIC_LIMIT:
                self.diagnostics_truncated = True
                del self._diagnostics[:-_DIAGNOSTIC_LIMIT]

    def __aiter__(self) -> Self:
        return self

    async def __anext__(self) -> bytes:
        try:
            line = await self.stdout.readline()
        except ValueError as error:
            message = "log record exceeds the configured byte limit"
            raise ValueError(message) from error
        if not line:
            code = await self.process.wait()
            await asyncio.shield(self._stderr_task)
            if code:
                raise LogProcessError(
                    self.command, code, self.diagnostics, self.diagnostics_truncated
                )
            raise StopAsyncIteration
        if len(line) > self.max_record_bytes:
            message = "log record exceeds the configured byte limit"
            raise ValueError(message)
        self._received += len(line)
        if self.max_output_bytes is not None and self._received > self.max_output_bytes:
            message = "log query exceeds the configured output byte limit"
            raise ValueError(message)
        return line

    async def close(self) -> None:
        if self.process.returncode is None:
            with suppress(ProcessLookupError):
                self.process.terminate()
        drained = asyncio.gather(
            self.process.wait(),
            _discard(self.stdout),
            self._stderr_task,
            return_exceptions=True,
        )
        try:
            async with asyncio.timeout(_CLOSE_TIMEOUT):
                outcomes = await asyncio.shield(drained)
        except TimeoutError:
            with suppress(ProcessLookupError):
                self.process.kill()
            outcomes = await drained
        for outcome in outcomes:
            if isinstance(outcome, BaseException):
                raise outcome


@asynccontextmanager
async def log_process(
    command: Sequence[str],
    *,
    max_record_bytes: int = 4 * 1024 * 1024,
    max_output_bytes: int | None = 64 * 1024 * 1024,
) -> AsyncIterator[LogOutput]:
    max_record_bytes = positive_bytes(max_record_bytes)
    if max_output_bytes is not None:
        max_output_bytes = positive_bytes(max_output_bytes)
    process = await create_subprocess_exec(
        *command,
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        limit=max_record_bytes,
    )
    output = LogOutput(process, tuple(command), max_record_bytes, max_output_bytes)
    try:
        yield output
    except BaseException as primary:
        try:
            await complete(output.close())
        except BaseException as error:
            raise primary from error
        raise
    else:
        await complete(output.close())
