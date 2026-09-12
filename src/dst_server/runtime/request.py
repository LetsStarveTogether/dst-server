from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass


@dataclass(slots=True)
class RequestState:
    sent: bool = False

    def mark_sent(self) -> None:
        self.sent = True

    def mark_rejected(self) -> None:
        self.sent = False


current_request = ContextVar[RequestState | None]("dst_server_request", default=None)


@contextmanager
def track_request(state: RequestState | None = None) -> Iterator[RequestState]:
    state = RequestState() if state is None else state
    token = current_request.set(state)
    try:
        yield state
    finally:
        current_request.reset(token)
