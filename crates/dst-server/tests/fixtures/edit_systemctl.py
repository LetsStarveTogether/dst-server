#!/usr/bin/env python3
"""Pause a host between reads to exercise edits from another host."""

import pathlib
import sys
import time

PAUSE_AFTER_SHOWS = 2

root = pathlib.Path(__file__).parent
if "show" in sys.argv:
    if pathlib.Path(__file__).name == "paused-systemctl":
        counter = root / "shows"
        count = int(counter.read_text()) + 1 if counter.exists() else 1
        counter.write_text(str(count))
        if count == PAUSE_AFTER_SHOWS:
            (root / "waiting").touch()
            deadline = time.monotonic() + 10
            while not (root / "release").exists():
                if time.monotonic() >= deadline:
                    message = "host edit barrier timed out"
                    raise SystemExit(message)
                time.sleep(0.01)
    active = (root / "service-state").read_text().strip()
    sys.stdout.write(
        f"Id=dst-001.service\nLoadState=loaded\nActiveState={active}\n"
        "SubState=dead\nJob=0\nResult=success\n"
    )
