from __future__ import annotations

import asyncio
import math
from collections.abc import Sequence
from datetime import datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING

from dst_server.rooms import Control, Room, read_control, write_control

if TYPE_CHECKING:
    from .service import Host

ANNOUNCE_MINUTES = 8
ANNOUNCE_TIMEOUT = 5


def local_now() -> datetime:
    return datetime.now().astimezone()


def is_open(definition: Room | Control, now: datetime) -> bool:
    if not definition.schedule:
        return True
    clock = now.time()
    return any(
        window.start <= clock < window.end
        if window.start < window.end
        else clock >= window.start or clock < window.end
        for window in definition.schedule
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


def record_transition(
    path: Path,
    definition: Room | Control,
    running: bool,
    *,
    override: bool,
    now: datetime | None = None,
) -> int:
    control = read_control(path)
    updates: dict[str, object] = {"revision": control.revision + 1}
    if override:
        updates.update(
            override=running, until=next_boundary(definition, now or local_now())
        )
    write_control(path, control.model_copy(update=updates))
    return control.revision + 1


def record_override(
    path: Path,
    definition: Room | Control,
    running: bool,
    now: datetime | None = None,
) -> None:
    record_transition(path, definition, running, override=True, now=now)


def clear_override(path: Path) -> int:
    control = read_control(path)
    write_control(
        path,
        control.model_copy(
            update={
                "override": None,
                "until": None,
                "revision": control.revision + 1,
            }
        ),
    )
    return control.revision + 1


def effective_state(
    path: Path, definition: Room | Control, now: datetime | None = None
) -> bool | None:
    control = read_control(path)
    if control.paused or not definition.schedule:
        return None
    current = now or local_now()
    if control.override is not None and (
        control.until is None or current < control.until
    ):
        return control.override
    return is_open(definition, current)


def next_close(
    path: Path, definition: Room | Control, now: datetime | None = None
) -> datetime | None:
    current = now or local_now()
    if effective_state(path, definition, current) is not True:
        return None
    return next(
        (
            boundary
            for boundary in boundaries(definition, current)
            if effective_state(path, definition, boundary) is False
        ),
        None,
    )


def shutdown_notice(
    path: Path, definition: Room | Control, now: datetime | None = None
) -> str | None:
    current = now or local_now()
    closing = next_close(path, definition, current)
    if closing is None:
        return None
    minutes = math.ceil((closing - current).total_seconds() / 60)
    if not 0 < minutes <= ANNOUNCE_MINUTES:
        return None
    opening = next_boundary(definition, closing)
    message = f"本房间将在约 {minutes} 分钟后定时关闭，请提前安排游戏进度。"  # ruff: ignore[ambiguous-unicode-character-string]
    if opening is not None:
        message += f"下次开放时间：{opening:%H:%M}。"  # ruff: ignore[ambiguous-unicode-character-string]
    return message


async def set_paused(
    host: Host, numbers: Sequence[int], paused: bool
) -> dict[int, dict[str, object]]:
    from .locking import room_lock

    results: dict[int, dict[str, object]] = {}
    for number in numbers:
        path = host.rooms.path(number)
        try:
            async with room_lock(path):
                host.rooms.policy(number)
                control = read_control(path)
                updated = control.model_copy(
                    update={
                        "paused": paused,
                        "override": None,
                        "until": None,
                        "revision": control.revision + 1,
                    }
                )
                write_control(path, updated)
        except Exception as error:
            results[number] = {"status": "failed", "error": str(error)}
        else:
            results[number] = {"status": "paused" if paused else "resumed"}
    return results


async def _announce_closure(
    host: Host, number: int, revision: int, now: datetime | None
) -> str | None:
    from .locking import room_lock

    path = host.rooms.path(number)
    async with room_lock(path):
        definition = host.rooms.policy(number)
        control = read_control(path)
        if control.revision != revision or not shutdown_notice(path, definition, now):
            return None
        async with asyncio.timeout(ANNOUNCE_TIMEOUT), host.connect(number) as client:
            instant = now or local_now()
            message = shutdown_notice(path, definition, instant)
            if not message:
                return None
            await client.announce(message)
        return message


async def _check_room(
    host: Host, number: int, now: datetime | None
) -> dict[str, object]:
    from .locking import room_lock

    path = host.rooms.path(number)
    async with room_lock(path):
        definition = host.rooms.policy(number)
        control = read_control(path)
        desired = effective_state(path, definition, now or local_now())
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
            number, override=False, wait=False, expected_revision=control.revision
        )
        return {"status": "stopped"}
    if desired and active == "inactive" and not job_id:
        await host.start(
            number, override=False, wait=False, expected_revision=control.revision
        )
        return {"status": "started"}
    result: dict[str, object] = {"status": "unchanged"}
    if (
        desired
        and active == "active"
        and not job_id
        and (message := await _announce_closure(host, number, control.revision, now))
    ):
        result["announcement"] = message
    return result


async def run_schedule(
    host: Host, now: datetime | None = None
) -> dict[int, dict[str, object]]:
    results: dict[int, dict[str, object]] = {}
    concurrency = asyncio.Semaphore(8)

    async def check(number: int) -> None:
        try:
            async with concurrency:
                results[number] = await _check_room(host, number, now)
        except Exception as error:
            results[number] = {"status": "failed", "error": str(error)}

    await asyncio.gather(*(check(number) for number in host.rooms.numbers()))
    return dict(sorted(results.items()))
