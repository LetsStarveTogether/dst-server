import asyncio

import pytest

from tests.helpers import wait_for_event


@pytest.mark.parametrize(
    "outcome", ["event", "error", "event-error", "early-return", "timeout"]
)
async def test_event_wait_observes_workers_and_reclaims_its_waiter(
    outcome: str,
) -> None:
    existing = asyncio.all_tasks()
    event = asyncio.Event()

    async def worker() -> None:
        if outcome == "event":
            event.set()
        elif outcome in {"error", "event-error"}:
            if outcome == "event-error":
                event.set()
            message = "worker failed"
            raise ValueError(message)
        elif outcome == "timeout":
            await asyncio.Event().wait()

    task = asyncio.create_task(worker())
    try:
        if outcome == "event":
            await wait_for_event(event, task)
        else:
            error = {
                "error": ValueError,
                "event-error": ValueError,
                "early-return": AssertionError,
                "timeout": TimeoutError,
            }[outcome]
            with pytest.raises(error):
                await wait_for_event(event, task, timeout=0.01)
        assert asyncio.all_tasks() <= existing | {task}
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
