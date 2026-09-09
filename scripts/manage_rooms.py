import logging
import sys
from collections.abc import Sequence
from datetime import datetime
from pathlib import Path

from pystemd.dbuslib import DBus
from pystemd.systemd1 import Manager

from scripts.generate_rooms import ROOM_NUMBERS, room_schedule

logger = logging.getLogger(__name__)


def is_open(number: int, now: datetime) -> bool:
    schedule = room_schedule(number)
    if schedule is None:
        return True
    _, start, end = schedule
    return (now.hour - start) % 24 < (end - start) % 24


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
        return check(manager, numbers, datetime.now().astimezone())


if __name__ == "__main__":
    sys.exit(main())
