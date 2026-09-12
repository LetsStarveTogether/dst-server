import asyncio
import os
import select
from typing import Any


async def wait_for_event(
    event: asyncio.Event,
    *tasks: asyncio.Task[Any],
    timeout: float = 5,  # ruff: ignore[async-function-with-timeout]
) -> None:
    """Reach a test milestone, surfacing a worker's early failure immediately."""
    waiter = asyncio.create_task(event.wait())
    try:
        async with asyncio.timeout(timeout):
            done, _ = await asyncio.wait(
                (waiter, *tasks), return_when=asyncio.FIRST_COMPLETED
            )
            for task in tasks:
                if task in done:
                    task.result()
            if event.is_set():
                return
            message = "worker finished before the expected event"
            raise AssertionError(message)
    finally:
        waiter.cancel()
        await asyncio.gather(waiter, return_exceptions=True)


def process_stopped(process_id: int) -> bool:
    try:
        descriptor = os.pidfd_open(process_id)
    except ProcessLookupError:
        return True
    try:
        readable, _, _ = select.select((descriptor,), (), (), 2)
        return bool(readable)
    finally:
        os.close(descriptor)


class BlockingProcess:
    pid = 1
    returncode = None

    def __init__(self) -> None:
        self.stdout = asyncio.StreamReader()
        self.stdout.feed_data(b"READY\n")

    async def wait(self) -> int:
        await asyncio.Event().wait()
        return 0
