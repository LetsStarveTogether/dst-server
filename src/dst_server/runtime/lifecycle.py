import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from time import time_ns

from logbook import Logger

from dst_server.events import server

from .fds import PROTOCOL_LINE_LIMIT

logger = Logger(__name__)

MAX_PENDING_EVENTS = 64


@dataclass(frozen=True, slots=True)
class ObservedLifecycleEvent:
    event: server.Event
    observed_timestamp_ns: int


@dataclass(slots=True)
class RequestStatus:
    sent: bool = False
    attempt: int = 0
    rejected: asyncio.Event = field(default_factory=asyncio.Event)


@dataclass(slots=True)
class RequestState:
    status: RequestStatus = field(default_factory=RequestStatus)
    resolved: asyncio.Event = field(default_factory=asyncio.Event)

    @property
    def sent(self) -> bool:
        return self.status.sent

    def mark_sent(self) -> None:
        self.status.sent = True
        self.status.attempt += 1
        self.status.rejected.clear()
        self.resolved.clear()

    def mark_rejected(self) -> None:
        self.status.sent = False
        self.status.rejected.set()

    async def wait_resolved(self) -> None:
        if self.resolved.is_set() or self.status.rejected.is_set():
            return
        resolved = asyncio.create_task(self.resolved.wait())
        rejected = asyncio.create_task(self.status.rejected.wait())
        try:
            await asyncio.wait(
                (resolved, rejected), return_when=asyncio.FIRST_COMPLETED
            )
        finally:
            for task in (resolved, rejected):
                if not task.done():
                    task.cancel()
            await asyncio.gather(resolved, rejected, return_exceptions=True)


async def read_line(reader: asyncio.StreamReader) -> bytes | None:
    oversized = False
    while True:
        try:
            line = await reader.readuntil(b"\n")
        except asyncio.LimitOverrunError as error:
            await reader.readexactly(min(error.consumed, PROTOCOL_LINE_LIMIT))
            oversized = True
            continue
        except asyncio.IncompleteReadError as error:
            if oversized or not error.partial:
                return None
            return error.partial
        if oversized:
            oversized = False
            continue
        return line


class Lifecycle:
    def __init__(self) -> None:
        self.queue: asyncio.Queue[ObservedLifecycleEvent] = asyncio.Queue(
            maxsize=MAX_PENDING_EVENTS
        )
        self.eof = False
        self.ready = False
        self.ready_or_eof = asyncio.Event()
        self.stopping = asyncio.Event()
        self.saved = asyncio.Event()
        self.save_lock = asyncio.Lock()
        self._save_confirmation_barrier: RequestState | None = None
        self._save_request: RequestState | None = None
        self._save_confirmation: tuple[server.SavedEvent, int] | None = None
        self.save_count = 0
        self.last_saved: server.SavedEvent | None = None
        self.session_id: str | None = None
        self.session_generation = 0

    async def pump(
        self,
        reader: asyncio.StreamReader,
        on_session: Callable[[int], None],
        on_event: Callable[[server.Event, int], Awaitable[None]] | None = None,
    ) -> None:
        try:
            while (line := await read_line(reader)) is not None:
                if line.startswith(b"DST_Stats|"):
                    continue
                observed_timestamp_ns = time_ns()
                event = server.parse_event(line.decode(errors="replace").rstrip("\r\n"))
                if __debug__:
                    logger.debug("DST server event : {event}", event=event)
                self.handle(event, on_session)
                if on_event is not None:
                    await on_event(event, observed_timestamp_ns)
                await self.queue.put(
                    ObservedLifecycleEvent(event, observed_timestamp_ns)
                )
        finally:
            self.close()

    def close(self) -> None:
        if self.eof:
            return
        self.eof = True
        self._resolve_save_barrier()
        self.ready_or_eof.set()
        self.saved.set()
        self.queue.shutdown()

    def handle(
        self,
        event: server.Event,
        on_session: Callable[[int], None],
    ) -> None:
        if self.eof:
            return
        if isinstance(event, (server.ReadyEvent, server.SessionEvent)):
            self.ready = True
            self.ready_or_eof.set()
        if isinstance(event, server.SessionEvent):
            self.session_id = event.session_id
            self.session_generation += 1
            on_session(self.session_generation)
        if isinstance(event, server.SavedEvent):
            self.last_saved = event
            self.save_count += 1
            self._resolve_save_barrier()
            if self._save_request is not None and self._save_request.sent:
                attempt = self._save_request.status.attempt
                if (
                    self._save_confirmation is None
                    or self._save_confirmation[1] != attempt
                ):
                    self._save_confirmation = (event, attempt)
            self.saved.set()
        if isinstance(event, server.StoppingEvent):
            self.stopping.set()

    def _resolve_save_barrier(self) -> None:
        if self._save_confirmation_barrier is not None:
            self._save_confirmation_barrier.resolved.set()

    def _discard_stale_confirmation(self, state: RequestState) -> None:
        if self._save_confirmation is not None and (
            not state.sent or self._save_confirmation[1] != state.status.attempt
        ):
            self._save_confirmation = None

    def _raise_if_eof(self) -> None:
        if self.eof:
            msg = "DST event stream closed before save completed"
            raise EOFError(msg)

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

    async def wait_for_save(
        self,
        request: Callable[[], Awaitable[None]],
        completion_timeout: float,
        request_state: RequestState | None = None,
    ) -> server.SavedEvent:
        async with self.save_lock:
            barrier = self._save_confirmation_barrier
            if barrier is not None:
                await barrier.wait_resolved()
                if self._save_confirmation_barrier is barrier:
                    self._save_confirmation_barrier = None
            self._raise_if_eof()
            state = request_state or RequestState(RequestStatus(sent=True))
            self._save_request = state
            self._save_confirmation = None
            self.saved.clear()
            try:  # ruff: ignore[too-many-statements-in-try-clause]
                await request()
                self._discard_stale_confirmation(state)
                async with asyncio.timeout(completion_timeout):
                    while self._save_confirmation is None:
                        self.saved.clear()
                        self._raise_if_eof()
                        if self._save_confirmation is None:
                            await self.saved.wait()
            except BaseException:
                self._discard_stale_confirmation(state)
                if self._save_confirmation is None and state.sent:
                    self._save_confirmation_barrier = state
                    if self.eof:
                        state.resolved.set()
                raise
            finally:
                confirmation = self._save_confirmation
                self._save_request = None
                self._save_confirmation = None
            if confirmation is None:
                msg = "DST reported a save without save metadata"
                raise RuntimeError(msg)
            return confirmation[0]
