import argparse
import asyncio
import logging
from bisect import bisect_left
from collections.abc import Sequence
from datetime import datetime, timedelta
from pathlib import Path

from pystemd.dbuslib import DBus
from pystemd.systemd1 import Manager

from dst_server.activity import read_last_login
from dst_server.models.cluster import ClusterStatus
from dst_server.rpc import ClusterClient, rpc_runtime
from scripts.manage_rooms import is_open

logger = logging.getLogger(__name__)


def idle_limit(day: int) -> timedelta:
    return timedelta(hours=(3, 12, 36, 120, 168)[bisect_left((8, 30, 70, 280), day)])


def online_rooms(manager: Manager, numbers: Sequence[int], now: datetime) -> set[int]:
    names = {
        f"dst-{number:03d}-pod.service".encode(): number
        for number in numbers
        if number in range(100) and is_open(number, now)
    }
    if not names:
        return set()
    return {
        names[row[0]]
        for row in manager.Manager.ListUnitsByNames(list(names))
        if row[2] == b"loaded" and row[3] == b"active" and not row[7]
    }


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


async def recycle(
    manager: Manager,
    client: ClusterClient,
    directory: Path,
    number: int,
    *,
    dry_run: bool = False,
) -> bool:
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
    if number not in online_rooms(manager, (number,), now):
        return False
    connected = await client.shard(status.master).connected_shards()
    if len(connected) != len(sessions) or not all(shard.ready for shard in connected):
        return False
    if (
        ready_sessions(await client.status()) != sessions
        or await client.list_players()
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
        await client.regenerate(
            expected_session_id=sessions[status.master], require_empty=True
        )
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


async def run(*, dry_run: bool = False) -> int:
    numbers = [
        number
        for number in range(100)
        if (Path("/etc/containers/systemd") / f"dst-{number:03d}.pod").is_file()
    ]
    failed = False
    with DBus() as bus:
        manager = Manager(bus=bus)
        manager.load()
        async with rpc_runtime():
            for number in sorted(
                online_rooms(manager, numbers, datetime.now().astimezone())
            ):
                directory = Path("/srv/dst") / f"{number:03d}"
                try:
                    async with await ClusterClient.connect(
                        directory / ".dst-server.sock"
                    ) as client:
                        await recycle(
                            manager, client, directory, number, dry_run=dry_run
                        )
                except Exception:
                    logger.exception("%03d: room recycling failed", number)
                    failed = True
    return int(failed)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Regenerate idle online DST rooms 000-099"
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="report eligible rooms without regenerating",
    )
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    return asyncio.run(run(dry_run=args.dry_run))


if __name__ == "__main__":
    raise SystemExit(main())
