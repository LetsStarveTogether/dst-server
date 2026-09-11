"""Scheduling, maintenance and process entry points."""

import asyncio
import logging
import math
import os
import re
import sys
from collections.abc import Mapping
from contextlib import aclosing
from datetime import time
from pathlib import Path
from typing import Annotated, Any, Literal

from cyclopts import App, Parameter
from cyclopts.types import PositiveInt
from pydantic import TypeAdapter

from dst_server.configuration.models import Port
from dst_server.rooms import Control, DailyWindow, write_control
from dst_server.timeouts import DEFAULT_LIFECYCLE_TIMEOUT, positive_timeout

from .common import BatchFailure, batch, emit, make_host, select_rooms

agent_app = App(name="agent", help="Run a game agent inside a container.")
schedule_app = App(name="schedule", help="Manage daily room opening hours.")
maintenance_app = App(
    name="maintenance", help="Run room maintenance and inspect background tasks."
)
_PORT = TypeAdapter(Port)
type AllRooms = Annotated[bool, Parameter(name="--all")]


def _port(_: object, value: int | None) -> None:
    if value is not None:
        _PORT.validate_python(value, strict=True)


type ExternalPort = Annotated[int | None, Parameter(validator=_port)]


@agent_app.command
async def prepare() -> None:
    """Prepare shared room files and download required mods."""
    from logbook import StreamHandler

    from dst_server.cluster.service import prepare_shared

    with StreamHandler(sys.stdout, format_string="{record.message}").applicationbound():
        await prepare_shared()


async def _serve(shard: str | None, external_port: int | None) -> int:
    from logbook import StreamHandler

    from dst_server.cluster import daemon
    from dst_server.telemetry import TelemetrySettings

    telemetry = TelemetrySettings.model_validate({
        "profile": os.environ.get("DST_SERVER_TELEMETRY_PROFILE", "critical")
    })
    with StreamHandler(sys.stdout, format_string="{record.message}").applicationbound():
        if shard is None:
            return await daemon.master(telemetry=telemetry, external_port=external_port)
        return await daemon.serve(
            shard=shard, telemetry=telemetry, external_port=external_port
        )


@agent_app.command
async def master(*, external_port: ExternalPort = None) -> int:
    """Serve the cluster controller and master shard."""
    return await _serve(None, external_port)


@agent_app.command
async def serve(shard: str, *, external_port: ExternalPort = None) -> int:
    """Serve one secondary shard."""
    return await _serve(shard, external_port)


@schedule_app.command(name="show")
async def show_schedule(
    *,
    room: tuple[str, ...] = (),
    all_rooms: AllRooms = False,
    template: str | None = None,
) -> None:
    """Show opening hours, manual overrides and the next schedule boundary."""
    from dst_server.host.schedule import (
        effective_state,
        local_now,
        next_boundary,
        read_control,
    )

    async with make_host() as host:
        numbers = select_rooms(host, room, all_rooms, template)
        now = local_now()
        results = []
        for number in numbers:
            definition = host.rooms.policy(number)
            path = host.rooms.path(number)
            results.append({
                "room": number,
                "windows": definition.schedule,
                "control": read_control(path),
                "open": effective_state(path, definition, now),
                "next_boundary": next_boundary(definition, now),
            })
        emit(results)


@schedule_app.command(name="set")
async def set_schedule(
    *windows: str,
    room: tuple[str, ...] = (),
    all_rooms: AllRooms = False,
    template: str | None = None,
    always: bool = False,
) -> None:
    """Set daily HH:MM-HH:MM windows, or --always for unscheduled operation."""
    if bool(windows) == always:
        msg = "provide daily windows or --always"
        raise ValueError(msg)
    from dst_server.host.locking import room_lock

    parsed = []
    for window in windows:
        match = re.fullmatch(r"([0-9]{2}:[0-9]{2})-([0-9]{2}:[0-9]{2})", window)
        if match is None:
            msg = f"invalid daily window: {window}; use HH:MM-HH:MM"
            raise ValueError(msg)
        parsed.append(
            DailyWindow(
                start=time(hour=int(match[1][:2]), minute=int(match[1][3:])),
                end=time(hour=int(match[2][:2]), minute=int(match[2][3:])),
            )
        )
    async with make_host() as host:

        async def save(number: int) -> Control:
            path = host.rooms.path(number)
            async with room_lock(path):
                previous = host.rooms.policy(number)
                updated = previous.model_copy(
                    update={
                        "schedule": tuple(parsed),
                        "override": None,
                        "until": None,
                        "revision": previous.revision + 1,
                    }
                )
                write_control(path, updated)
                return updated

        await batch(select_rooms(host, room, all_rooms, template), save)


def _emit_results(results: Mapping[Any, object]) -> None:
    emit(results)
    rooms = results.get("rooms", results)
    if not isinstance(rooms, Mapping):
        rooms = results
    if any(
        isinstance(item, dict) and item.get("status") == "failed"
        for item in rooms.values()
    ):
        raise BatchFailure


async def _pause(
    paused: bool, room: tuple[str, ...], all_rooms: bool, template: str | None
) -> None:
    from dst_server.host.schedule import set_paused

    async with make_host() as host:
        numbers = select_rooms(host, room, all_rooms, template)
        _emit_results(await set_paused(host, numbers, paused))


@schedule_app.command
async def pause(
    *,
    room: tuple[str, ...] = (),
    all_rooms: AllRooms = False,
    template: str | None = None,
) -> None:
    """Suspend automatic management until explicitly resumed."""
    await _pause(True, room, all_rooms, template)


@schedule_app.command
async def resume(
    *,
    room: tuple[str, ...] = (),
    all_rooms: AllRooms = False,
    template: str | None = None,
) -> None:
    """Resume the configured schedule and clear manual overrides."""
    await _pause(False, room, all_rooms, template)


@schedule_app.command(name="run")
async def run_schedule() -> None:
    """Apply opening hours and send upcoming closure announcements once."""
    from dst_server.host.schedule import run_schedule

    async with make_host() as host:
        _emit_results(await run_schedule(host))


def duration(value: str) -> float:
    """Parse finite nonnegative seconds, optionally suffixed with s, m or h."""
    match = re.fullmatch(r"([0-9]+(?:\.[0-9]+)?(?:[eE][+-]?[0-9]+)?)([smh]?)", value)
    if match is None:
        msg = "duration must be seconds or a number followed by s, m or h"
        raise ValueError(msg)
    seconds = float(match[1]) * {"": 1, "s": 1, "m": 60, "h": 3600}[match[2]]
    if not math.isfinite(seconds):
        msg = "duration must be finite"
        raise ValueError(msg)
    return seconds


@maintenance_app.command(name="restart")
async def maintenance_restart(
    *,
    room: tuple[str, ...] = (),
    all_rooms: AllRooms = False,
    template: str | None = None,
    delay: str = "8m",
    detach: bool = False,
    timeout: float = DEFAULT_LIFECYCLE_TIMEOUT,  # ruff: ignore[async-function-with-timeout]
) -> None:
    """Announce a countdown, then restart the selected rooms."""
    from dst_server.host.maintenance import maintain_restart

    seconds = duration(delay)
    timeout = positive_timeout(timeout)
    async with make_host() as host:
        _emit_results(
            await maintain_restart(
                host,
                select_rooms(host, room, all_rooms, template),
                delay=seconds,
                detach=detach,
                timeout=timeout,
            ),
        )


@maintenance_app.command
async def recycle(*, dry_run: bool = False) -> None:
    """Regenerate eligible empty rooms using their configured recycling policy."""
    from dst_server.host.recycling import run_recycle

    async with make_host() as host:
        _emit_results(await run_recycle(host, dry_run=dry_run))


@maintenance_app.command(name="status")
async def maintenance_status(task: str) -> None:
    """Read a background maintenance task's current or retained completion status."""
    from dst_server.host.maintenance import task_status

    async with make_host() as host:
        emit(await task_status(host, task))


@maintenance_app.command(name="cancel")
async def maintenance_cancel(task: str) -> None:
    """Cancel a background maintenance task and any unstarted room restarts."""
    from dst_server.host.maintenance import cancel_task

    async with make_host() as host:
        emit(await cancel_task(host, task))


@maintenance_app.command(name="logs")
async def maintenance_logs(
    task: str, *, follow: bool = False, lines: int = 100
) -> None:
    """Read retained maintenance task logs and optionally follow new records."""
    from dst_server.host.logs import logs
    from dst_server.host.maintenance import task_unit

    async with aclosing(logs((task_unit(task),), follow=follow, lines=lines)) as stream:
        async for record in stream:
            emit(record)


async def annotations(
    input: Path,
    *,
    output: Path | None = None,
    max_workers: PositiveInt | None = None,
    mode: Literal["auto", "components", "modutil"] = "auto",
) -> int:
    """Generate Lua language-server annotations."""
    from dst_server.annotations import generate_components, generate_modutil

    if mode == "auto":
        if await asyncio.to_thread(input.is_dir):
            mode = "components"
        elif "modutil" in input.name.casefold() and await asyncio.to_thread(
            input.is_file
        ):
            mode = "modutil"
        else:
            msg = "cannot infer annotation mode; pass --mode"
            raise ValueError(msg)
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    if mode == "components":
        content = await asyncio.to_thread(generate_components, input, max_workers)
        default_output = f"{input.name}_def.lua"
    else:
        content = await asyncio.to_thread(generate_modutil, input)
        default_output = f"{input.stem}_def.lua"
    if not content:
        msg = f"No Lua annotation definitions generated from {input}"
        raise ValueError(msg)
    destination = output or Path(default_output)
    await asyncio.to_thread(destination.write_text, content, encoding="utf-8")
    logging.getLogger(__name__).info("Output written to %s", destination)
    return 0


def completion(shell: Literal["bash", "zsh", "fish"]) -> None:
    """Print shell completion without changing shell configuration."""
    from . import app

    sys.stdout.write(app.generate_completion(shell=shell))
