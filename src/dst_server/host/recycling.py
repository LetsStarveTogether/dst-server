"""Persist room activity and regenerate empty worlds after their retention period."""

import asyncio
import logging
from bisect import bisect_left
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING

from dst_server.activity import ActivityCheckpoint
from dst_server.concurrency import cancel_tasks
from dst_server.models.cluster import ClusterStatus
from dst_server.rooms import read_control, write_control
from dst_server.rpc import ClusterClient

from .locking import RoomBusyError, room_lock
from .schedule import is_open

if TYPE_CHECKING:
    from dst_server.rooms import Control

    from .service import Host

logger = logging.getLogger(__name__)


def idle_limit(day: int) -> timedelta:
    return timedelta(hours=(6, 24, 36, 72, 168)[bisect_left((8, 30, 70, 280), day)])


def _enabled(definition: Control, now: datetime) -> bool:
    return definition.recycle and not definition.paused and is_open(definition, now)


async def online(host: Host, number: int) -> bool:
    unit = await host.status(number, game=False)
    return (
        unit["load"] == "loaded" and unit["active"] == "active" and not unit["job_id"]
    )


def ready_sessions(status: ClusterStatus) -> dict[str, str]:
    if status.phase != "running" or status.missing_shards:
        return {}
    sessions = {}
    for shard in status.shards:
        if (
            shard.phase != "running"
            or shard.desired != "running"
            or not shard.ready
            or not shard.session_id
            or shard.driver_error
        ):
            return {}
        sessions[shard.name] = shard.session_id
    return sessions if status.master in sessions else {}


async def recycle(  # ruff: ignore[complex-structure]
    host: Host,
    client: ClusterClient,
    directory: Path,
    number: int,
    *,
    dry_run: bool = False,
) -> bool:
    status = await client.status()
    sessions = ready_sessions(status)
    if not sessions:
        return False
    occupied = bool(await client.list_players())
    day = (await client.shard(status.master).world()).day
    now = datetime.now().astimezone()
    pending = None
    try:
        async with room_lock(directory):
            control = read_control(directory)
            previous = control.activity
            # ponytail: no event journal; an abrupt exit can lose activity since
            # the previous host tick. Missing/new worlds get a full grace period.
            last_active = (
                previous.last_active_at
                if previous is not None and previous.sessions == sessions
                else now
            )
            last_active = max(
                last_active,
                *(shard.last_active_at or last_active for shard in status.shards),
                now if occupied else last_active,
            )
            checkpoint = ActivityCheckpoint(
                sessions=sessions, last_active_at=last_active.astimezone(UTC)
            )
            if not dry_run and checkpoint != previous:
                write_control(
                    directory, control.model_copy(update={"activity": checkpoint})
                )
            if (
                occupied
                or status.busy
                or not _enabled(control, now)
                or now - last_active <= idle_limit(day)
            ):
                return False
        connected = await client.shard(status.master).connected_shards()
        if len(connected) != len(sessions) or not all(
            shard.ready for shard in connected
        ):
            return False
        if not await online(host, number):
            return False
        async with room_lock(directory):
            if not _enabled(read_control(directory), datetime.now().astimezone()):
                return False
            logger.info(
                "%03d: %s day=%s last_active=%s",
                number,
                "would regenerate" if dry_run else "regenerate",
                day,
                last_active.isoformat(),
            )
            if not dry_run:
                # Submit while locked; generation itself never holds the host lock.
                pending = asyncio.create_task(
                    client.regenerate(
                        expected_session_id=sessions[status.master], require_empty=True
                    ),
                    eager_start=True,
                )
        if pending is not None:
            await pending
    finally:
        if pending is not None:
            await cancel_tasks(pending)
    if pending is not None:
        current = ready_sessions(await client.status())
        if current.keys() != sessions.keys() or any(
            current[shard] == session for shard, session in sessions.items()
        ):
            msg = (
                f"{number:03d}: regeneration did not confirm new worlds on every shard"
            )
            raise RuntimeError(msg)
        async with room_lock(directory):
            control = read_control(directory)
            write_control(
                directory,
                control.model_copy(
                    update={
                        "activity": ActivityCheckpoint(
                            sessions=current, last_active_at=datetime.now(UTC)
                        )
                    }
                ),
            )
        logger.info("%03d: regeneration complete", number)
    return True


async def run_recycle(
    host: Host, *, dry_run: bool = False, numbers: Sequence[int] | None = None
) -> dict[int, dict[str, str]]:
    """Observe active recycle-enabled rooms, including manually paused rooms."""
    selected = sorted(set(numbers)) if numbers is not None else host.rooms.numbers()
    results = {}
    for number in selected:
        changed = False
        try:
            if host.rooms.policy(number).recycle and await online(host, number):
                async with host.connect(number) as client:
                    changed = await recycle(
                        host, client, host.rooms.path(number), number, dry_run=dry_run
                    )
            status = (
                ("would-regenerate" if dry_run else "regenerated")
                if changed
                else "skipped"
            )
            results[number] = {"status": status}
        except RoomBusyError:
            results[number] = {"status": "skipped"}
        except Exception as error:
            logger.exception("%03d: room recycling failed", number)
            results[number] = {"status": "failed", "error": str(error)}
    return results
