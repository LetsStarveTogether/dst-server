from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable

from logbook import Logger

from .events import (
    ServerEvent,
    ServerReadyEvent,
    ServerSavedEvent,
    ServerSessionEvent,
    ServerStoppingEvent,
    parse_server_event,
)

logger = Logger(__name__)


class ServerEventStream:
    def __init__(self) -> None:
        self.queue: asyncio.Queue[ServerEvent | None] = asyncio.Queue()
        self.eof = False
        self.ready = False
        self.ready_or_eof = asyncio.Event()
        self.stopping = asyncio.Event()
        self.saved = asyncio.Event()
        self.save_count = 0
        self.last_saved: ServerSavedEvent | None = None
        self.session_id: str | None = None
        self.session_generation = 0

    async def pump(
        self,
        reader: asyncio.StreamReader,
        on_session: Callable[[int], None],
    ) -> None:
        try:
            while line := await reader.readline():
                event = parse_server_event(line.decode(errors="replace").rstrip("\r\n"))
                if __debug__:
                    logger.debug("DST server event : {event}", event=event)
                self.handle(event, on_session)
        finally:
            self.eof = True
            self.ready_or_eof.set()
            self.queue.put_nowait(None)

    def handle(
        self,
        event: ServerEvent,
        on_session: Callable[[int], None],
    ) -> None:
        if isinstance(event, (ServerReadyEvent, ServerSessionEvent)):
            self.ready = True
            self.ready_or_eof.set()
        if isinstance(event, ServerSessionEvent):
            self.session_id = event.session_id
            self.session_generation += 1
            on_session(self.session_generation)
        if isinstance(event, ServerSavedEvent):
            self.last_saved = event
            self.save_count += 1
            self.saved.set()
        if isinstance(event, ServerStoppingEvent):
            self.stopping.set()
        self.queue.put_nowait(event)

    async def wait_ready(self) -> None:
        await self.ready_or_eof.wait()
        if not self.ready:
            msg = "DST event stream closed before the server became ready"
            raise EOFError(msg)

    async def read(self) -> ServerEvent | None:
        if self.eof and self.queue.empty():
            return None
        return await self.queue.get()

    async def wait_for_save(
        self,
        request: Callable[[], Awaitable[None]],
        completion_timeout: float,
    ) -> ServerSavedEvent:
        save_count = self.save_count
        await request()
        async with asyncio.timeout(completion_timeout):
            while self.save_count == save_count:
                self.saved.clear()
                if self.save_count == save_count:
                    await self.saved.wait()
        if self.last_saved is None:
            msg = "DST reported a save without save metadata"
            raise RuntimeError(msg)
        return self.last_saved


__all__ = ["ServerEventStream"]
