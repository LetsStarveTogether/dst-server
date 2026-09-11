from __future__ import annotations

import asyncio
import json
import math
import os
import re
import signal
import sys
from collections.abc import Sequence
from typing import TYPE_CHECKING, Any
from uuid import uuid4

from dst_server.configuration.files import atomic_write, read_text
from dst_server.rooms import control_revision
from dst_server.timeouts import DEFAULT_LIFECYCLE_TIMEOUT, positive_timeout

from .schedule import ANNOUNCE_TIMEOUT

if TYPE_CHECKING:
    from .service import Host

TASK_PREFIX = "dst-maintenance-"
_TASK_NAME = re.compile(r"dst-maintenance-[0-9a-f]{32}\.service\Z")


def task_unit(task: str) -> str:
    unit = task if task.endswith(".service") else task + ".service"
    if not _TASK_NAME.fullmatch(unit):
        msg = "invalid DST maintenance task ID"
        raise ValueError(msg)
    return unit


def _finish(
    host: Host, results: dict[int, dict[str, object]], status: str | None = None
) -> None:
    if task := os.environ.get("DST_MAINTENANCE_TASK"):
        task = task_unit(task)
        path = host.cluster_root / ".dst-maintenance" / f"{task}.json"
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        atomic_write(
            path,
            json.dumps({
                "task": task,
                "status": status
                or (
                    "failed"
                    if any(value["status"] == "failed" for value in results.values())
                    else "completed"
                ),
                "rooms": results,
            })
            + "\n",
            0o600,
        )


def _revisions(host: Host, numbers: Sequence[int]) -> dict[int, int | dict[str, str]]:
    if not os.environ.get("DST_MAINTENANCE_TASK"):
        snapshots: dict[int, int | dict[str, str]] = {}
        for number in numbers:
            try:
                host.rooms.policy(number)
                snapshots[number] = control_revision(host.rooms.path(number))
            except Exception as error:
                snapshots[number] = {"error": str(error)}
        return snapshots
    encoded = os.environ.get("DST_MAINTENANCE_REVISIONS", "")
    try:
        values = json.loads(encoded)
    except json.JSONDecodeError as error:
        msg = "invalid detached maintenance revisions"
        raise ValueError(msg) from error
    if (
        not isinstance(values, dict)
        or set(values) != {str(number) for number in numbers}
        or any(
            value is not None and not (type(value) is int and value >= 0)
            for value in values.values()
        )
    ):
        msg = "detached maintenance revisions must match every selected room"
        raise ValueError(msg)
    return {
        int(number): revision
        if revision is not None
        else {"error": "room failed validation when maintenance was submitted"}
        for number, revision in values.items()
    }


async def maintain_restart(  # ruff: ignore[complex-structure, too-many-locals, too-many-statements]
    host: Host,
    numbers: Sequence[int],
    *,
    delay: float = 480,
    detach: bool = False,
    timeout: float = DEFAULT_LIFECYCLE_TIMEOUT,  # ruff: ignore[async-function-with-timeout]
) -> dict[int, dict[str, object]] | dict[str, object]:
    if not math.isfinite(delay) or delay < 0:
        msg = "maintenance delay must be finite and nonnegative"
        raise ValueError(msg)
    timeout = positive_timeout(timeout)
    numbers = tuple(dict.fromkeys(numbers))
    if not numbers:
        msg = "maintenance requires at least one room"
        raise ValueError(msg)
    snapshots = _revisions(host, numbers)
    results: dict[int, dict[str, object]] = {
        number: {"status": "failed", "error": value["error"]}
        for number, value in snapshots.items()
        if isinstance(value, dict)
    }
    revisions = {
        number: value for number, value in snapshots.items() if isinstance(value, int)
    }
    if detach and revisions:
        unit = f"{TASK_PREFIX}{uuid4().hex}.service"
        argv = [
            sys.executable,
            "-m",
            "dst_server",
            "--cluster-root",
            str(host.cluster_root),
            "--quadlet-dir",
            str(host.quadlet_dir),
            "--json",
            "maintenance",
            "restart",
            "--room",
            ",".join(str(number) for number in numbers),
            "--delay",
            str(delay),
            "--timeout",
            str(timeout),
        ]
        await host.systemd.start_transient(
            unit,
            argv,
            environment={
                "DST_MAINTENANCE_TASK": unit,
                "DST_MAINTENANCE_REVISIONS": json.dumps({
                    number: value if isinstance(value, int) else None
                    for number, value in snapshots.items()
                }),
            },
        )
        return {
            "task": unit,
            "status": "submitted",
            "rooms": {
                number: results.get(number, {"status": "submitted"})
                for number in numbers
            },
        }

    concurrency = asyncio.Semaphore(8)
    restarting: set[int] = set()
    deadline = asyncio.get_running_loop().time() + delay

    async def action(number: int, minutes: int | None = None) -> None:
        try:  # ruff: ignore[too-many-statements-in-try-clause]
            async with concurrency:
                host.rooms.policy(number)
                if control_revision(host.rooms.path(number)) != revisions[number]:
                    results[number] = {"status": "superseded"}
                    return
                if minutes is not None:
                    message = (
                        f"本房间将在约 {minutes} 分钟后维护重启，请提前安排游戏进度。"  # ruff: ignore[ambiguous-unicode-character-string]
                    )
                    async with asyncio.timeout(ANNOUNCE_TIMEOUT):
                        await host.announce(number, message)
                else:
                    restarting.add(number)
                    await host.restart(
                        number,
                        override=False,
                        expected_revision=revisions[number],
                        timeout=timeout,
                    )
                    results[number] = {"status": "restarted"}
        except Exception as error:
            superseded = str(error) == "room operation was superseded"
            results[number] = {
                "status": "superseded" if superseded else "failed",
                "error": str(error),
            }

    loop = asyncio.get_running_loop()
    previous_handler = signal.getsignal(signal.SIGTERM)
    task = asyncio.current_task()
    detached_worker = bool(os.environ.get("DST_MAINTENANCE_TASK"))
    if detached_worker and task is not None:
        loop.add_signal_handler(signal.SIGTERM, task.cancel)
    cancelled = False
    try:  # ruff: ignore[too-many-statements-in-try-clause]
        while (remaining := deadline - asyncio.get_running_loop().time()) > 0:
            minutes = math.ceil(remaining / 60)
            await asyncio.gather(
                *(
                    action(number, minutes)
                    for number in numbers
                    if number not in results
                )
            )
            if len(results) == len(numbers):
                break
            next_notice = deadline - (minutes - 1) * 60
            await asyncio.sleep(max(0, next_notice - asyncio.get_running_loop().time()))
        await asyncio.gather(
            *(action(number) for number in numbers if number not in results)
        )
    except asyncio.CancelledError:
        cancelled = True
        raise
    finally:
        if detached_worker:
            loop.remove_signal_handler(signal.SIGTERM)
            signal.signal(signal.SIGTERM, previous_handler)
        if cancelled:
            for number in numbers:
                results.setdefault(
                    number,
                    {
                        "status": "indeterminate"
                        if number in restarting
                        else "cancelled"
                    },
                )
            _finish(host, results, "cancelled")
    _finish(host, results)
    return dict(sorted(results.items()))


async def task_status(host: Host, task: str) -> dict[str, Any]:
    unit = task_unit(task)
    properties = {}
    try:
        properties = await host.systemd.properties(unit)
    except Exception as error:
        if not any(
            name in str(error) for name in ("NoSuchUnit", "not loaded", "not found")
        ):
            raise
    else:
        if properties.get("ActiveState") in {
            "active",
            "activating",
            "deactivating",
        }:
            return {
                "task": unit,
                "status": properties["ActiveState"],
                "properties": properties,
            }
    try:
        return json.loads(
            read_text(host.cluster_root / ".dst-maintenance" / f"{unit}.json")
        )
    except FileNotFoundError:
        pass
    terminal: dict[str, Any] = {
        "task": unit,
        "status": "finished-or-unavailable",
        "rooms": {},
    }
    if properties.get("ActiveState") == "failed":
        terminal.update(status="failed", properties=properties)
    return terminal


async def cancel_task(host: Host, task: str) -> dict[str, str]:
    unit = task_unit(task)
    await host.systemd.stop(unit)
    return {"task": unit, "status": "cancellation-requested"}
