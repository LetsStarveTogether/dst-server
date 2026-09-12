from __future__ import annotations

import asyncio
import math
from collections.abc import Sequence
from datetime import datetime, timedelta
from typing import TYPE_CHECKING

from dst_server.announcements import Template, maintenance
from dst_server.rooms import Control, Room, write_control

if TYPE_CHECKING:
    from .service import Host

ANNOUNCE_MINUTES = 8
ANNOUNCE_TIMEOUT = 5


def local_now() -> datetime:
    return datetime.now().astimezone()


def is_open(definition: Room | Control, now: datetime) -> bool:
    return not definition.schedule or any(
        window.contains(now.time()) for window in definition.schedule
    )


def boundaries(definition: Room | Control, now: datetime) -> tuple[datetime, ...]:
    # Filtering actual transitions merges touching and overlapping daily windows.
    candidates = {
        datetime.combine(now.date() + timedelta(days=day), endpoint, now.tzinfo)
        for day in range(3)
        for window in definition.schedule
        for endpoint in (window.start, window.end)
    }
    return tuple(
        candidate
        for candidate in sorted(candidates)
        if candidate > now
        and is_open(definition, candidate - timedelta(microseconds=1))
        != is_open(definition, candidate)
    )


def next_boundary(definition: Room | Control, now: datetime) -> datetime | None:
    return next(iter(boundaries(definition, now)), None)


def effective_state(definition: Control, now: datetime | None = None) -> bool | None:
    if definition.paused or not definition.schedule:
        return None
    return is_open(definition, now or local_now())


def shutdown_notice(definition: Control, now: datetime | None = None) -> str | None:
    current = now or local_now()
    if effective_state(definition, current) is not True:
        return None
    closing = next_boundary(definition, current)
    if closing is None:
        return None
    minutes = math.ceil((closing - current).total_seconds() / 60)
    if not 0 < minutes <= ANNOUNCE_MINUTES:
        return None
    opening = next_boundary(definition, closing)
    return maintenance(
        Template.SCHEDULED_CLOSE,
        next_opening=opening.strftime("%H:%M") if opening is not None else None,
    ).render((closing - current).total_seconds())


async def set_paused(
    host: Host, numbers: Sequence[int], paused: bool
) -> dict[int, dict[str, object]]:
    from .locking import room_lock

    results: dict[int, dict[str, object]] = {}
    for number in numbers:
        path = host.rooms.path(number)
        try:
            async with room_lock(path):
                control = host.rooms.policy(number)
                updated = control.model_copy(update={"paused": paused})
                write_control(path, updated)
        except Exception as error:
            results[number] = {"status": "failed", "error": str(error)}
        else:
            results[number] = {"status": "paused" if paused else "resumed"}
    return results


async def _check_room(
    host: Host, number: int, now: datetime | None
) -> dict[str, object]:
    from .locking import room_lock

    path = host.rooms.path(number)
    async with room_lock(path):
        control = host.rooms.policy(number)
        desired = effective_state(control, now)
    if desired is None:
        return {"status": "paused" if control.paused else "unmanaged"}
    status = await host.status(number, game=False)
    load, active = status["load"], status["active"]
    job_id, job_type = status["job_id"], status["job_type"]
    if load == "masked" or (job_id and job_type == "stop"):
        return {"status": "unchanged"}
    if load != "loaded" or active == "failed":
        msg = f"load={load} active={active}"
        raise RuntimeError(msg)
    if not desired and (
        active in {"active", "activating"}
        or (job_id and job_type in {"start", "restart"})
    ):
        await host.stop(
            number,
            automatic=True,
            wait=False,
        )
        return {"status": "stopped"}
    if desired and active == "inactive" and not job_id:
        await host.start(number, automatic=True, wait=False)
        return {"status": "started"}
    result: dict[str, object] = {"status": "unchanged"}
    if desired and active == "active" and not job_id:
        async with room_lock(path):
            definition = host.rooms.policy(number)
            message = shutdown_notice(definition, now)
            if message:
                async with (
                    asyncio.timeout(ANNOUNCE_TIMEOUT),
                    host.connect(number) as client,
                ):
                    message = shutdown_notice(definition, now)
                    if message:
                        await client.announce(message)
                        result["announcement"] = message
    return result


async def run_schedule(
    host: Host, now: datetime | None = None
) -> dict[int, dict[str, object]]:
    results: dict[int, dict[str, object]] = {}
    concurrency = asyncio.Semaphore(8)
    from .locking import RoomBusyError

    async def check(number: int) -> None:
        try:
            async with concurrency:
                results[number] = await _check_room(host, number, now)
        except RoomBusyError:
            results[number] = {"status": "busy"}
        except Exception as error:
            results[number] = {"status": "failed", "error": str(error)}

    await asyncio.gather(*(check(number) for number in host.rooms.numbers()))
    return dict(sorted(results.items()))
