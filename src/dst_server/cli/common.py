# ruff: file-ignore[print]
"""CLI context, room selection and output policy."""

import asyncio
import dataclasses
import json
import re
from collections.abc import Awaitable, Callable, Sequence
from contextvars import ContextVar
from pathlib import Path
from typing import TYPE_CHECKING, Any

from pydantic import TypeAdapter
from rich.console import Console
from rich.table import Table

if TYPE_CHECKING:
    from dst_server.host import Host

MAX_ROOM_NUMBER = 299


@dataclasses.dataclass(frozen=True)
class Options:
    cluster_root: Path = Path("/srv/dst")
    quadlet_dir: Path = Path("/etc/containers/systemd")
    json: bool = False


options = ContextVar[Options | None]("cli_options", default=None)


def context() -> Options:
    return options.get() or Options()


def make_host() -> Host:
    from dst_server.host import Host

    settings = context()
    return Host(settings.cluster_root, settings.quadlet_dir)


def parse_rooms(rooms: Sequence[str]) -> tuple[int, ...]:
    result: set[int] = set()
    for item in rooms:
        for part in item.split(","):
            if not re.fullmatch(r"\d{1,3}(?:-\d{1,3})?", part):
                msg = f"invalid room selection: {part!r}"
                raise ValueError(msg)
            bounds = tuple(map(int, part.split("-")))
            start, end = bounds[0], bounds[-1]
            if not 0 <= start <= end <= MAX_ROOM_NUMBER:
                msg = "room numbers must be between 000 and 299, in ascending ranges"
                raise ValueError(msg)
            result.update(range(start, end + 1))
    return tuple(sorted(result))


def select_rooms(
    host: Any,
    rooms: tuple[str, ...] = (),
    all_rooms: bool = False,
    template: str | None = None,
) -> tuple[int, ...]:
    if all_rooms and rooms:
        msg = "choose explicit rooms or --all"
        raise ValueError(msg)
    if not rooms and not all_rooms and template is None:
        msg = "select --room, --template or --all explicitly"
        raise ValueError(msg)
    numbers = parse_rooms(rooms) if rooms else host.rooms.numbers()
    selected = []
    for number in numbers:
        if template is not None:
            try:
                matches = host.rooms.policy(number).template == template
            except Exception as error:
                msg = f"cannot filter room {number:03d} by template: {error}"
                raise ValueError(msg) from error
            if not matches:
                continue
        selected.append(number)
    if not selected:
        msg = "no rooms matched the selection"
        raise ValueError(msg)
    return tuple(selected)


def emit(value: Any) -> None:
    value = TypeAdapter(Any).dump_python(
        value,
        mode="json",
        fallback=lambda item: TypeAdapter(type(item)).dump_python(item, mode="json"),
    )
    if context().json:
        print(json.dumps(value, ensure_ascii=False, allow_nan=False))
        return
    console = Console(highlight=False)
    if (
        isinstance(value, list)
        and value
        and all(isinstance(item, dict) for item in value)
    ):
        keys = tuple(dict.fromkeys(key for item in value for key in item))
        table = Table(*keys)
        for row in value:
            table.add_row(*(str(row.get(key, "")) for key in keys))
        console.print(table)
    elif isinstance(value, str):
        console.print(value, markup=False)
    else:
        console.print_json(json.dumps(value, ensure_ascii=False, allow_nan=False))


class BatchFailure(Exception):  # ruff: ignore[error-suffix-on-exception-name]
    pass


async def batch(
    numbers: Sequence[int],
    operation: Callable[[int], Awaitable[Any]],
    *,
    render: Callable[[Sequence[dict[str, Any]]], None] | None = None,
) -> None:
    limit = asyncio.Semaphore(8)

    async def one(number: int) -> dict[str, Any]:
        async with limit:
            try:
                result = await operation(number)
            except Exception as error:
                record = {"room": number, "ok": False, "error": str(error)}
                if hasattr(error, "result"):
                    record["result"] = error.result
                return record
            else:
                return {"room": number, "ok": True, "result": result}

    results = await asyncio.gather(*(one(number) for number in numbers))
    if render is not None and not context().json:
        render(results)
    else:
        emit(results)
    if any(not item["ok"] for item in results):
        raise BatchFailure
