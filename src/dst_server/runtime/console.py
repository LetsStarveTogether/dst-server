from __future__ import annotations

import asyncio
from time import time_ns

from dst_server.telemetry.stream import EventStream

COMMAND_DONE = "DST_RemoteCommandDone"
LUA_BUSY = "DST_LuaBusy"
LUA_BUSY_RETRY_DELAY = 0.1


class LuaBusyError(Exception):
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

    async def execute(self, command: str) -> str:
        if "\n" in command or "\r" in command:
            msg = "DST console commands must be a single line"
            raise ValueError(msg)
        async with self.lock:
            await self.drain_result()
            while True:
                try:
                    return await self.execute_once(command)
                except LuaBusyError:
                    await asyncio.sleep(LUA_BUSY_RETRY_DELAY)

    async def execute_once(self, command: str) -> str:
        self.writer.write(f"{command}\n".encode())
        result_task = asyncio.create_task(self.read_result())
        self.pending_result = result_task
        try:
            await self.writer.drain()
            return await asyncio.shield(result_task)
        finally:
            if result_task.done():
                self.pending_result = None

    async def read_result(self) -> str:
        lines = []
        while line := await self.reader.readline():
            observed_timestamp_ns = time_ns()
            value = line.decode(errors="replace").rstrip("\r\n")
            if value == COMMAND_DONE:
                return "\n".join(lines)
            if value == LUA_BUSY:
                raise LuaBusyError
            if self.game_events.accept(value, observed_timestamp_ns):
                continue
            lines.append(value)
        msg = "DST result stream closed before DST_RemoteCommandDone"
        raise EOFError(msg)

    async def drain_result(self) -> None:
        result_task = self.pending_result
        if result_task is None:
            return
        try:
            await asyncio.shield(result_task)
        except LuaBusyError:
            pass
        finally:
            if result_task.done():
                self.pending_result = None

    async def close(self) -> None:
        self.writer.close()
        await asyncio.gather(self.writer.wait_closed(), return_exceptions=True)


__all__ = ["Console", "LuaBusyError"]
