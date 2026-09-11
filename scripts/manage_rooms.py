import asyncio
import logging
import sys
from collections.abc import Sequence
from datetime import datetime
from itertools import starmap
from pathlib import Path

from pystemd.dbuslib import DBus
from pystemd.systemd1 import Manager

from dst_server.presets.lst import ROOM_NUMBERS, room_schedule
from dst_server.rpc import ClusterClient, rpc_runtime

logger = logging.getLogger(__name__)
ANNOUNCE_MINUTES = 8
ANNOUNCE_TIMEOUT = 5


def is_open(number: int, now: datetime) -> bool:
    schedule = room_schedule(number)
    if schedule is None:
        return True
    _, start, end = schedule
    return (now.hour - start) % 24 < (end - start) % 24


def shutdown_notice(number: int, now: datetime) -> str | None:
    schedule = room_schedule(number)
    if schedule is None or not is_open(number, now):
        return None
    _, start, end = schedule
    minutes = 60 - now.minute
    if (now.hour + 1) % 24 == end and minutes <= ANNOUNCE_MINUTES:
        return (
            f"本房间将在约 {minutes} 分钟后定时关闭，请提前安排游戏进度。"  # ruff: ignore[ambiguous-unicode-character-string]
            f"下次开放时间：{start:02d}:00。"  # ruff: ignore[ambiguous-unicode-character-string]
        )
    return None


async def announce_closures(
    manager: Manager, numbers: Sequence[int], now: datetime
) -> int:
    rooms = {
        f"dst-{number:03d}-pod.service".encode(): (number, message)
        for number in numbers
        if (message := shutdown_notice(number, now)) is not None
    }
    if not rooms:
        return 0
    online = [
        rooms[row[0]]
        for row in manager.Manager.ListUnitsByNames(list(rooms))
        if row[2] == b"loaded" and row[3] == b"active" and not row[7]
    ]
    if not online:
        return 0

    async def announce(number: int, message: str) -> bool:
        try:
            async with asyncio.timeout(ANNOUNCE_TIMEOUT):
                async with await ClusterClient.connect(
                    Path("/srv/dst") / f"{number:03d}" / ".dst-server.sock",
                    timeout=ANNOUNCE_TIMEOUT,
                ) as client:
                    if shutdown_notice(number, datetime.now().astimezone()) != message:
                        return False
                    await client.announce(message)
            logger.info("%03d: %s", number, message)
        except Exception:
            logger.exception("%03d: shutdown announcement failed", number)
            return True
        return False

    async with rpc_runtime():
        return int(any(await asyncio.gather(*starmap(announce, online))))


def check(manager: Manager, numbers: Sequence[int], now: datetime) -> int:
    rooms = {
        f"dst-{number:03d}-pod.service".encode(): number
        for number in numbers
        if room_schedule(number) is not None
    }
    if not rooms:
        return 0

    stops: list[bytes] = []
    starts: list[bytes] = []
    failed = False
    for row in manager.Manager.ListUnitsByNames(list(rooms)):
        name, _, load, active, *_ = row
        job_id, job_type = row[7:9]
        if load == b"masked" or (job_id and job_type == b"stop"):
            continue
        if load != b"loaded" or active == b"failed":
            logger.error("%s: load=%s active=%s", name.decode(), load, active)
            failed = True
        elif active == b"inactive" and not job_id and is_open(rooms[name], now):
            starts.append(name)
        elif (
            active in {b"active", b"activating"}
            or (job_id and job_type in {b"start", b"restart"})
        ) and not is_open(rooms[name], now):
            stops.append(name)

    for action, names in (("StopUnit", stops), ("StartUnit", starts)):
        for name in names:
            try:
                job = getattr(manager.Manager, action)(name, b"replace")
                logger.info("%s %s: %s", action, name.decode(), job.decode())
            except Exception:
                logger.exception("%s %s failed", action, name.decode())
                failed = True
    return int(failed)


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    directory = Path("/etc/containers/systemd")
    numbers = [
        number
        for number in ROOM_NUMBERS
        if (directory / f"dst-{number:03d}.pod").is_file()
    ]
    with DBus() as bus:
        manager = Manager(bus=bus)
        manager.load()
        now = datetime.now().astimezone()
        failed = check(manager, numbers, now)
        asyncio.run(announce_closures(manager, numbers, now))
        return failed


if __name__ == "__main__":
    sys.exit(main())
