import asyncio
from typing import Annotated

from pydantic import Field, TypeAdapter

from dst_server.errors import SubscriptionOverflowError

MAX_BATCH_SIZE = 256
BATCH_SIZE = TypeAdapter(Annotated[int, Field(ge=1, le=MAX_BATCH_SIZE)])


class Broadcast[T]:
    def __init__(self) -> None:
        self._subscriptions: set[Subscription[T]] = set()
        self._closed = False

    def subscribe(self) -> Subscription[T]:
        if self._closed:
            msg = "broadcast is closed"
            raise RuntimeError(msg)
        subscription = Subscription(self)
        self._subscriptions.add(subscription)
        return subscription

    def publish(self, item: T) -> None:
        for subscription in tuple(self._subscriptions):
            subscription._publish(item)  # ruff: ignore[private-member-access]

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        for subscription in tuple(self._subscriptions):
            subscription.close()

    def _discard(self, subscription: Subscription[T]) -> None:
        self._subscriptions.discard(subscription)


class Subscription[T]:
    def __init__(self, owner: Broadcast[T]) -> None:
        self._owner = owner
        self._queue: asyncio.Queue[T] = asyncio.Queue(maxsize=1024)
        self._closed = False
        self._reading = False
        self._overflowed = False

    async def next(self, max_items: int) -> tuple[T, ...]:
        max_items = BATCH_SIZE.validate_python(max_items, strict=True)
        if self._reading:
            msg = "concurrent subscription reads are unsupported"
            raise RuntimeError(msg)
        self._reading = True
        try:
            if self._overflowed:
                self._overflowed = False
                msg = "subscription queue overflowed"
                raise SubscriptionOverflowError(msg)
            try:
                first = await self._queue.get()
            except asyncio.QueueShutDown:
                return ()
            if self._overflowed:
                self._overflowed = False
                msg = "subscription queue overflowed"
                raise SubscriptionOverflowError(msg)

            items = [first]
            while len(items) < max_items:
                try:
                    items.append(self._queue.get_nowait())
                except asyncio.QueueEmpty, asyncio.QueueShutDown:
                    break
            return tuple(items)
        finally:
            self._reading = False

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._overflowed = False
        self._owner._discard(self)  # ruff: ignore[private-member-access]
        self._queue.shutdown(immediate=True)

    def _publish(self, item: T) -> None:
        try:
            self._queue.put_nowait(item)
        except asyncio.QueueFull:
            self._overflowed = True
            for _ in range(self._queue.qsize()):
                self._queue.get_nowait()
            self._queue.put_nowait(item)
        except asyncio.QueueShutDown:
            pass
