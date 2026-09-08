import asyncio
from weakref import ref

import pytest
from pydantic import ValidationError

from dst_server.cluster.subscriptions import (
    MAX_BATCH_SIZE,
    Broadcast,
)
from dst_server.errors import SubscriptionOverflowError


def test_abandoned_subscription_releases_queued_records() -> None:
    class Record:
        pass

    broadcast = Broadcast[Record]()
    subscription = broadcast.subscribe()
    record = Record()
    subscription_reference, record_reference = ref(subscription), ref(record)
    broadcast.publish(record)

    del record, subscription

    assert subscription_reference() is None
    assert record_reference() is None
    assert not broadcast._subscriptions


async def test_subscription_waits_and_drains_bounded_batches() -> None:
    broadcast = Broadcast[int]()
    broadcast.publish(0)
    subscription = broadcast.subscribe()
    reading = asyncio.create_task(subscription.next(2))
    await asyncio.sleep(0)

    for item in range(1, 4):
        broadcast.publish(item)

    assert await reading == (1, 2)
    assert await subscription.next(MAX_BATCH_SIZE) == (3,)


async def test_overflow_is_local_reported_once_and_recovers() -> None:
    broadcast = Broadcast[int]()
    slow, healthy = broadcast.subscribe(), broadcast.subscribe()

    for item in range(1024):
        broadcast.publish(item)
    for start in range(0, 1024, MAX_BATCH_SIZE):
        assert await healthy.next(MAX_BATCH_SIZE) == tuple(
            range(start, start + MAX_BATCH_SIZE)
        )
    broadcast.publish(1024)

    with pytest.raises(SubscriptionOverflowError):
        await slow.next(1)
    assert await slow.next(1) == (1024,)
    assert await healthy.next(1) == (1024,)

    broadcast.publish(1025)
    assert await slow.next(1) == (1025,)


async def test_close_wakes_reader_and_is_idempotent() -> None:
    broadcast = Broadcast[int]()
    subscription = broadcast.subscribe()
    reading = asyncio.create_task(subscription.next(1))
    await asyncio.sleep(0)

    subscription.close()
    subscription.close()
    broadcast.publish(1)

    assert await reading == ()
    assert await subscription.next(1) == ()

    broadcast.close()
    broadcast.close()
    with pytest.raises(RuntimeError, match="closed"):
        broadcast.subscribe()


async def test_concurrent_reads_are_rejected() -> None:
    subscription = Broadcast[int]().subscribe()
    reading = asyncio.create_task(subscription.next(1))
    await asyncio.sleep(0)

    with pytest.raises(RuntimeError, match="concurrent"):
        await subscription.next(1)
    subscription.close()
    assert await reading == ()


@pytest.mark.parametrize("max_items", [True, 1.5, "1", 0, MAX_BATCH_SIZE + 1])
async def test_batch_size_is_validated(max_items: object) -> None:
    broadcast = Broadcast[int]()
    subscription = broadcast.subscribe()
    broadcast.publish(1)
    with pytest.raises(ValidationError):
        await subscription.next(max_items)  # ty: ignore[invalid-argument-type]
