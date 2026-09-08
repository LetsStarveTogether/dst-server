import asyncio

import pytest

from dst_server.concurrency import cancel_tasks, complete


@pytest.mark.parametrize("cancellations", [0, 1, 3])
async def test_completion_finishes_before_propagating_cancellation(
    cancellations: int,
) -> None:
    entered, release, finished = (asyncio.Event() for _ in range(3))

    async def operation() -> int:
        entered.set()
        await release.wait()
        finished.set()
        return 42

    pending = asyncio.create_task(complete(operation()))
    try:
        async with asyncio.timeout(2):
            await entered.wait()
            for _ in range(cancellations):
                pending.cancel()
                await asyncio.sleep(0)
                assert not pending.done()
            release.set()
            if cancellations:
                with pytest.raises(asyncio.CancelledError):
                    await pending
            else:
                assert await pending == 42
            assert finished.is_set()
    finally:
        release.set()
        await asyncio.gather(pending, return_exceptions=True)


@pytest.mark.parametrize(
    "error", [ValueError("cleanup failed"), asyncio.CancelledError()]
)
async def test_completion_preserves_operation_failure(error: BaseException) -> None:
    future = asyncio.get_running_loop().create_future()
    future.set_exception(error)
    with pytest.raises(type(error)) as caught:
        await complete(future)
    assert caught.value is error


async def test_cancellation_preserves_a_later_cleanup_failure_as_its_cause() -> None:
    entered, release = asyncio.Event(), asyncio.Event()
    failure = OSError("cleanup failed")

    async def operation() -> None:
        entered.set()
        await release.wait()
        raise failure

    pending = asyncio.create_task(complete(operation()))
    try:
        async with asyncio.timeout(2):
            await entered.wait()
            pending.cancel("original cancellation")
            await asyncio.sleep(0)
            pending.cancel("second cancellation")
            await asyncio.sleep(0)
            assert not pending.done()
            release.set()
            with pytest.raises(asyncio.CancelledError) as caught:
                await pending
            assert caught.value.args == ("original cancellation",)
            assert caught.value.__cause__ is failure
    finally:
        release.set()
        await asyncio.gather(pending, return_exceptions=True)


async def test_cancel_tasks_finishes_all_finalizers_under_repeated_cancellation() -> (
    None
):
    entered = [asyncio.Event(), asyncio.Event()]
    finalizing = [asyncio.Event(), asyncio.Event()]
    finished: list[int] = []
    release = asyncio.Event()

    async def operation(index: int) -> None:
        try:
            entered[index].set()
            await asyncio.Event().wait()
        finally:
            finalizing[index].set()
            await release.wait()
            finished.append(index)

    tasks = tuple(asyncio.create_task(operation(index)) for index in range(2))
    pending: asyncio.Task[None] | None = None
    try:
        async with asyncio.timeout(2):
            await asyncio.gather(*(event.wait() for event in entered))
            pending = asyncio.create_task(cancel_tasks(*tasks))
            await asyncio.gather(*(event.wait() for event in finalizing))
            for _ in range(3):
                pending.cancel()
                await asyncio.sleep(0)
                assert not pending.done()
            release.set()
            with pytest.raises(asyncio.CancelledError):
                await pending
            assert sorted(finished) == [0, 1]
            assert all(task.cancelled() for task in tasks)
    finally:
        release.set()
        await asyncio.gather(*tasks, return_exceptions=True)
        if pending is not None:
            await asyncio.gather(pending, return_exceptions=True)


async def test_cancel_tasks_accepts_empty_and_completed_work() -> None:
    await cancel_tasks()
    done = asyncio.get_running_loop().create_future()
    done.set_result(1)
    await cancel_tasks(done)
    assert done.result() == 1


@pytest.mark.parametrize("closers", [1, 3])
async def test_cancel_tasks_waits_for_existing_cancellation_without_reinjecting(
    closers: int,
) -> None:
    entered, finalizing, release, finished = (asyncio.Event() for _ in range(4))

    async def operation() -> None:
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            finalizing.set()
            await release.wait()
            finished.set()

    task = asyncio.create_task(operation())
    pending = asyncio.get_running_loop().create_future()
    closing: list[asyncio.Task[None]] = []
    try:
        await entered.wait()
        task.cancel()
        await finalizing.wait()
        closing = [
            asyncio.create_task(cancel_tasks(task, pending)) for _ in range(closers)
        ]
        await asyncio.sleep(0)
        release.set()
        await asyncio.gather(*closing)
        assert task.cancelled()
        assert pending.cancelled()
        assert finished.is_set()
    finally:
        release.set()
        await asyncio.gather(task, *closing, return_exceptions=True)
