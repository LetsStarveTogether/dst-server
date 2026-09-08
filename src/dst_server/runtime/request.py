import asyncio
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field


@dataclass(slots=True)
class RequestState:
    sent: bool = False
    attempt: int = 0
    resolved: asyncio.Event = field(default_factory=asyncio.Event)

    def mark_sent(self) -> None:
        self.sent = True
        self.attempt += 1
        self.resolved.clear()

    def mark_rejected(self) -> None:
        self.sent = False
        self.resolved.set()


current_request = ContextVar[RequestState | None]("dst_server_request", default=None)


@contextmanager
def track_request(state: RequestState | None = None) -> Iterator[RequestState]:
    state = RequestState() if state is None else state
    token = current_request.set(state)
    try:
        yield state
    finally:
        current_request.reset(token)
