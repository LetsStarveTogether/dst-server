import asyncio
from collections.abc import Awaitable
from typing import Any


async def complete[T](operation: Awaitable[T]) -> T:
    """Finish an owned operation before propagating caller cancellation."""
    task = asyncio.ensure_future(operation)
    cancelled: asyncio.CancelledError | None = None
    while not task.done():
        try:
            await asyncio.wait((task,))
        except asyncio.CancelledError as error:
            cancelled = cancelled or error
    try:
        result = task.result()
    except BaseException as error:
        if cancelled is not None:
            raise cancelled from error
        raise
    if cancelled is not None:
        raise cancelled
    return result


async def cancel_tasks(*tasks: asyncio.Future[Any]) -> None:
    """Cancel owned tasks and wait for all their finalizers."""
    for task in tasks:
        if not task.done() and not (
            isinstance(task, asyncio.Task) and task.cancelling()
        ):
            task.cancel()
    if tasks:
        await complete(asyncio.gather(*tasks, return_exceptions=True))
