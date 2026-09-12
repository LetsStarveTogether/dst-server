from __future__ import annotations

import asyncio
from collections.abc import Sequence
from typing import TYPE_CHECKING

from dst_server.announcements import Template, maintenance
from dst_server.timeouts import DEFAULT_LIFECYCLE_TIMEOUT, positive_timeout

if TYPE_CHECKING:
    from .service import Host


async def maintain_restart(
    host: Host,
    numbers: Sequence[int],
    *,
    delay: float = 480,
    reason: Template = Template.RESTART,
    estimated_duration: float = 300,
    timeout: float = DEFAULT_LIFECYCLE_TIMEOUT,  # ruff: ignore[async-function-with-timeout]
) -> dict[int, dict[str, object]]:
    """Submit one in-service game restart per room, retaining no task state."""
    notice = maintenance(
        reason, delay=delay, interval=60, estimated_duration=estimated_duration
    )
    timeout = positive_timeout(timeout)
    numbers = tuple(dict.fromkeys(numbers))
    if not numbers:
        msg = "maintenance requires at least one room"
        raise ValueError(msg)
    results: dict[int, dict[str, object]] = {}
    concurrency = asyncio.Semaphore(8)

    async def restart(number: int) -> None:
        try:
            async with concurrency, host.connect(number) as client:
                await client.restart(notice=notice, timeout=timeout)
            results[number] = {"status": "restarted"}
        except Exception as error:
            results[number] = {"status": "failed", "error": str(error)}

    await asyncio.gather(*(restart(number) for number in numbers))
    return dict(sorted(results.items()))
