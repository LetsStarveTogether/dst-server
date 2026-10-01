#!/usr/bin/env python3
"""Disposable systemd process fixture with cross-process concurrency counts."""

import fcntl
import json
import sys
import time
from pathlib import Path

root = Path(__file__).resolve().parent


def count(change: int) -> None:
    with (root / "systemd-counts.json").open("r+") as stream:
        fcntl.flock(stream, fcntl.LOCK_EX)
        counts = json.load(stream)
        counts["active"] += change
        counts["peak"] = max(counts["peak"], counts["active"])
        counts["started" if change > 0 else "finished"] += 1
        stream.seek(0)
        json.dump(counts, stream)
        stream.truncate()


count(1)
try:
    time.sleep(0.005)
    if "show" in sys.argv:
        unit = sys.argv[-1]
        active = (root / "services-active").exists()
        sys.stdout.write(
            f"Id={unit}\nLoadState=loaded\n"
            f"ActiveState={'active' if active else 'inactive'}\n"
            f"SubState={'running' if active else 'dead'}\nJob=0\nResult=success\n"
        )
finally:
    count(-1)
