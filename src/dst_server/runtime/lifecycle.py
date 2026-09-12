import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from time import time_ns

from logbook import Logger

from dst_server.events import server

from .fds import read_line

logger = Logger(__name__)

MAX_PENDING_EVENTS = 64


@dataclass(frozen=True, slots=True)
class ObservedLifecycleEvent:
    event: server.Event
    observed_timestamp_ns: int


class Lifecycle:
    def __init__(self) -> None:
        self.queue: asyncio.Queue[ObservedLifecycleEvent] = asyncio.Queue(
            maxsize=MAX_PENDING_EVENTS
        )
        self.dropped = 0
        self.eof = False
        self.ready = False
        self.ready_or_eof = asyncio.Event()
        self.stopping = asyncio.Event()
        self.session_id: str | None = None

    async def pump(
        self,
        reader: asyncio.StreamReader,
        on_event: Callable[[server.Event, int], Awaitable[None]] | None = None,
    ) -> None:
        try:
            while True:
                line, oversized = await read_line(reader)
                if line is None:
                    break
                if oversized or line.startswith(b"DST_Stats|"):
                    del line
                    continue
                observed_timestamp_ns = time_ns()
                event = server.parse_event(line.decode(errors="replace").rstrip("\r\n"))
                del line
                if __debug__:
                    logger.debug("DST server event : {event}", event=event)
                self.handle(event)
                if on_event is not None:
                    await on_event(event, observed_timestamp_ns)
                if self.queue.full():
                    self.queue.get_nowait()
                    self.dropped += 1
                    if self.dropped & (self.dropped - 1) == 0:
                        logger.warning(
                            "DST lifecycle notification queue dropped {count} records",
                            count=self.dropped,
                        )
                self.queue.put_nowait(
                    ObservedLifecycleEvent(event, observed_timestamp_ns)
                )
                del event
        finally:
            self.close()

    def close(self) -> None:
        if self.eof:
            return
        self.eof = True
        self.ready_or_eof.set()
        self.queue.shutdown()

    def handle(self, event: server.Event) -> None:
        if self.eof:
            return
        if isinstance(event, (server.ReadyEvent, server.SessionEvent)):
            self.ready = True
            self.ready_or_eof.set()
        if isinstance(event, server.SessionEvent):
            self.session_id = event.session_id
        if isinstance(event, server.StoppingEvent):
            self.stopping.set()

    async def wait_ready(self) -> None:
        await self.ready_or_eof.wait()
        if self.eof:
            msg = "DST event stream closed before the server became ready"
            raise EOFError(msg)

    async def read(self) -> server.Event | None:
        observed = await self.read_observed()
        return observed.event if observed is not None else None

    async def read_observed(self) -> ObservedLifecycleEvent | None:
        try:
            return await self.queue.get()
        except asyncio.QueueShutDown:
            return None
