"""Subscribe to gameplay and capture changes before a journal backfill."""

import asyncio
from collections.abc import AsyncGenerator, AsyncIterator
from contextlib import AsyncExitStack, asynccontextmanager
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from typing import Any, Literal, Self

import dst_server

from ._native import decode_live_batch
from .events import SubscriptionBatch
from .history import (
    CoverageChange,
    EventFilter,
    HistoryPage,
    HistoryRecord,
    _observation,
    read_history,
)
from .host import Host

type LiveStream = Literal["events", "lifecycle"]


@dataclass(frozen=True, slots=True)
class LiveHistoryBatch:
    """A transport error ends its stream. A native final batch may contain records."""

    room: int
    stream: LiveStream
    observations: tuple[HistoryRecord | CoverageChange, ...]
    auxiliary: tuple[dict[str, Any], ...]
    rejections: tuple[dict[str, Any], ...]
    dropped: int | None
    closed: bool
    error: Exception | None = None
    observation_indices: tuple[int, ...] = ()
    auxiliary_indices: tuple[int, ...] = ()


_STREAMS: tuple[LiveStream, ...] = ("events", "lifecycle")
_MAX_BATCH = 512


type _Queue = asyncio.Queue[LiveHistoryBatch]


async def _pump(
    room: int,
    stream: LiveStream,
    sub: dst_server.Subscription,
    predicate: dict[str, Any],
    queue: _Queue,
    max_items: int,
) -> None:
    def decode(batch: SubscriptionBatch) -> LiveHistoryBatch:
        decoded = decode_live_batch(batch["records"], predicate, stream)
        rows = tuple(
            _observation(row, f"dst-{room:03}.service")
            for row in decoded["observations"]
        )
        return LiveHistoryBatch(
            room,
            stream,
            rows,
            tuple(decoded["auxiliary"]),
            tuple(decoded["rejections"]),
            batch["dropped"],
            batch["closed"],
            observation_indices=tuple(decoded["observation_indices"]),
            auxiliary_indices=tuple(decoded["auxiliary_indices"]),
        )

    while True:
        batch = None
        try:
            batch = await sub.next(max_items)
            result = decode(batch)
        except Exception as exc:
            result = LiveHistoryBatch(
                room,
                stream,
                (),
                (),
                (),
                batch["dropped"] if batch is not None else None,
                batch["closed"] if batch is not None else False,
                exc,
            )
        await queue.put(result)
        if result.closed or result.error is not None:
            return


class LiveHistory:
    """The two subscriptions are active when this object is returned."""

    def __init__(
        self, host: Host, room: int, where: EventFilter, queue: _Queue
    ) -> None:
        self.registered_at = datetime.now(UTC)
        self._queue = queue
        self._host = host
        self._room = room
        self._where = where
        self._closed: set[LiveStream] = set()

    def backfill(
        self,
        since: datetime,
        *,
        until: datetime,
        namespace: str | None = None,
        page_size: int = 500,
    ) -> AsyncIterator[HistoryPage]:
        """Read a journal receipt window [since, until) for reconciliation by UID.

        A record published before registration can reach the journal afterwards.
        Advance to elapsed receipt times on subsequent reads to pick up delayed records.
        Returns:
            Pages of journal entries visible in that window.
        """
        return read_history(
            self._host,
            [self._room],
            since=since,
            until=until,
            where=self._where,
            namespace=namespace,
            page_size=page_size,
        )

    def __aiter__(self) -> Self:
        return self

    async def __anext__(self) -> LiveHistoryBatch:
        try:
            batch = await self._queue.get()
        except asyncio.QueueShutDown:
            raise StopAsyncIteration from None
        if batch.closed or batch.error is not None:
            self._closed.add(batch.stream)
            if len(self._closed) == len(_STREAMS):
                self._queue.shutdown()
        return batch


@asynccontextmanager
async def subscribe_history(
    host: Host,
    room: int,
    *,
    where: EventFilter | None = None,
    buffer_batches: int = 8,
    max_items: int = 128,
) -> AsyncGenerator[LiveHistory]:
    """Drain both streams into a bounded queue with their native drop counts."""
    if type(buffer_batches) is not int or buffer_batches < 1:
        message = "buffer_batches must be a positive integer"
        raise ValueError(message)
    if type(max_items) is not int or not 1 <= max_items <= _MAX_BATCH:
        message = f"max_items must be an integer in 1..{_MAX_BATCH}"
        raise ValueError(message)
    if where is None:
        where = EventFilter()
    elif not isinstance(where, EventFilter):
        message = "where must be an EventFilter or None"
        raise TypeError(message)
    predicate = asdict(where)
    queue: _Queue = asyncio.Queue(buffer_batches)
    tasks = []
    async with AsyncExitStack() as stack:
        client = await stack.enter_async_context(host.connect(room))
        try:
            for stream in _STREAMS:
                sub = await stack.enter_async_context(await client.subscribe(stream))
                tasks.append(
                    asyncio.create_task(
                        _pump(room, stream, sub, predicate, queue, max_items)
                    )
                )
            yield LiveHistory(host, room, where, queue)
        finally:
            for task in tasks:
                task.cancel()
            queue.shutdown(immediate=True)
            await asyncio.gather(*tasks, return_exceptions=True)
