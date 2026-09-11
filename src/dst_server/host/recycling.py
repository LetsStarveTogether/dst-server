"""Regenerate configured rooms only after idle and world state checks."""

import asyncio
import logging
from bisect import bisect_left
from collections.abc import Sequence
from datetime import datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING

from dst_server.activity import read_last_login
from dst_server.concurrency import cancel_tasks
from dst_server.models.cluster import ClusterStatus
from dst_server.rooms import control_revision, read_control
from dst_server.rpc import ClusterClient

from .locking import room_lock
from .schedule import effective_state

if TYPE_CHECKING:
    from dst_server.rooms import Control

    from .service import Host

logger = logging.getLogger(__name__)


def idle_limit(day: int) -> timedelta:
    return timedelta(hours=(3, 12, 36, 120, 168)[bisect_left((8, 30, 70, 280), day)])


def _enabled(definition: Control, directory: Path, now: datetime) -> bool:
    if not definition.recycle or read_control(directory).paused:
        return False
    state = effective_state(directory, definition, now)
    return state is not False and (state is not None or not definition.schedule)


async def online(host: Host, number: int, now: datetime) -> bool:
    if not _enabled(host.rooms.policy(number), host.rooms.path(number), now):
        return False
    unit = await host.status(number, game=False)
    return (
        unit["load"] == "loaded" and unit["active"] == "active" and not unit["job_id"]
    )


def ready_sessions(status: ClusterStatus) -> dict[str, str]:
    if status.phase != "running" or status.missing_shards:
        return {}
    sessions = {}
    for shard in status.shards:
        health = shard.driver_health
        if (
            shard.phase != "running"
            or shard.desired != "running"
            or not shard.ready
            or not shard.session_id
            or shard.driver_error
        ):
            return {}
        if (
            health is None
            or health.errors
            or health.telemetry_status not in {"active", "disabled"}
            or shard.telemetry_invalid
            or shard.telemetry_dropped
        ):
            return {}
        sessions[shard.name] = shard.session_id
    return sessions if status.master in sessions else {}


async def recycle(  # ruff: ignore[complex-structure, too-many-branches]
    host: Host,
    client: ClusterClient,
    directory: Path,
    number: int,
    *,
    dry_run: bool = False,
) -> bool:
    revision = control_revision(directory)
    status = await client.status()
    sessions = ready_sessions(status)
    if not sessions or await client.list_players():
        return False
    logins = {
        shard: read_last_login(directory / shard, session)
        for shard, session in sessions.items()
    }
    last_login = max(
        (value for value in logins.values() if value is not None), default=None
    )
    if last_login is None:
        return False
    day = (await client.shard(status.master).world()).day
    now = datetime.now().astimezone()
    if now - last_login <= idle_limit(day):
        return False
    if not await online(host, number, now):
        return False
    connected = await client.shard(status.master).connected_shards()
    if len(connected) != len(sessions) or not all(shard.ready for shard in connected):
        return False
    if ready_sessions(await client.status()) != sessions or await client.list_players():
        return False
    if not await online(host, number, datetime.now().astimezone()):
        return False
    pending = None
    try:
        async with room_lock(directory):
            if (
                control_revision(directory) != revision
                or not _enabled(
                    host.rooms.policy(number), directory, datetime.now().astimezone()
                )
                or logins
                != {
                    shard: read_last_login(directory / shard, session)
                    for shard, session in sessions.items()
                }
            ):
                return False
            logger.info(
                "%03d: %s day=%s last_login=%s",
                number,
                "would regenerate" if dry_run else "regenerate",
                day,
                last_login.isoformat(),
            )
            if not dry_run:
                # ClusterClient sends before its first suspension. Eager execution
                # holds the lock through submission, never through world generation.
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
        regenerated = await client.status()
        current = ready_sessions(regenerated)
        if current.keys() != sessions.keys() or any(
            current[shard] == session for shard, session in sessions.items()
        ):
            msg = (
                f"{number:03d}: regeneration did not confirm new worlds on every shard"
            )
            raise RuntimeError(msg)
        logger.info("%03d: regeneration complete", number)
    return True


async def run_recycle(
    host: Host, *, dry_run: bool = False, numbers: Sequence[int] | None = None
) -> dict[int, dict[str, str]]:
    """Recycle eligible rooms, preserving each room's outcome on partial failure."""
    selected = sorted(set(numbers)) if numbers is not None else host.rooms.numbers()
    results = {}
    for number in selected:
        changed = False
        try:
            if await online(host, number, datetime.now().astimezone()):
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
        except Exception as error:
            logger.exception("%03d: room recycling failed", number)
            results[number] = {"status": "failed", "error": str(error)}
    return results
