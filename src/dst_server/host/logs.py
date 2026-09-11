import asyncio
import json
from asyncio import create_subprocess_exec
from collections.abc import AsyncGenerator, Sequence
from contextlib import suppress
from datetime import UTC, datetime, timedelta
from typing import cast

from pydantic import AwareDatetime, TypeAdapter

from dst_server.concurrency import complete
from dst_server.deployment.models import UnitName
from dst_server.models.base import FrozenModel

_STDERR_LIMIT = 8192
_RECORD_LIMIT = 64 * 1024 * 1024
_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)
_UNIT_NAME = TypeAdapter(UnitName)


class JournalRecord(FrozenModel):
    cursor: str
    timestamp: AwareDatetime
    unit: str
    message: str


def _text(value: object) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        if all(type(item) is int for item in value):
            return bytes(value).decode(errors="replace")
        return "\n".join(_text(item) for item in value)
    msg = "invalid journalctl field value"
    raise ValueError(msg)


def _record(line: bytes) -> JournalRecord:
    try:
        fields = json.loads(line)
        return JournalRecord(
            cursor=fields["__CURSOR"],
            timestamp=_EPOCH
            + timedelta(microseconds=int(_text(fields["__REALTIME_TIMESTAMP"]))),
            unit=_text(fields.get("UNIT") or fields.get("_SYSTEMD_UNIT")),
            message=_text(fields.get("MESSAGE")),
        )
    except (KeyError, TypeError, ValueError, OverflowError) as error:
        msg = "invalid journalctl JSON record"
        raise ValueError(msg) from error


async def _stderr(stream: asyncio.StreamReader) -> bytes:
    diagnostics = bytearray()
    while chunk := await stream.read(_STDERR_LIMIT):
        diagnostics.extend(chunk)
        del diagnostics[:-_STDERR_LIMIT]
    return bytes(diagnostics)


async def _discard(stream: asyncio.StreamReader) -> None:
    while await stream.read(65536):
        pass


async def _close(
    process: asyncio.subprocess.Process,
    diagnostics: asyncio.Task[bytes],
) -> None:
    stdout = cast(asyncio.StreamReader, process.stdout)
    stderr = cast(asyncio.StreamReader, process.stderr)
    if process.returncode is None:
        with suppress(ProcessLookupError):
            process.terminate()
    try:
        async with asyncio.timeout(3):
            await asyncio.gather(process.wait(), _discard(stdout), diagnostics)
    except TimeoutError:
        with suppress(ProcessLookupError):
            process.kill()
        await asyncio.gather(process.wait(), _discard(stdout), _discard(stderr))


async def logs(
    units: Sequence[str],
    *,
    lines: int | None = 100,
    follow: bool = False,
    since: str | None = None,
    until: str | None = None,
    after_cursor: str | None = None,
) -> AsyncGenerator[JournalRecord]:
    """Read retained journal history and optionally follow it in one process.

    Consume with ``contextlib.aclosing`` when breaking out before EOF.
    Date filters use journalctl's native syntax, including relative times.
    Pass ``lines=None`` to read all retained matching records.

    Yields:
        Journal records ordered from oldest to newest.

    Raises:
        ValueError: The query or a journal record is invalid.
        RuntimeError: The journal reader exits with an error.
    """
    if isinstance(units, str) or not units:
        msg = "journal logs require at least one explicit unit"
        raise ValueError(msg)
    if lines is not None and (type(lines) is not int or lines < 0):
        msg = "journal log lines must be a non-negative integer"
        raise ValueError(msg)
    command = [
        "journalctl",
        "--no-pager",
        "--all",
        "--output=json",
        "--output-fields=__CURSOR,__REALTIME_TIMESTAMP,UNIT,_SYSTEMD_UNIT,MESSAGE",
        f"--lines={lines if lines is not None else 'all'}",
        *(
            f"--unit={_UNIT_NAME.validate_python(unit)}"
            for unit in dict.fromkeys(units)
        ),
    ]
    if follow:
        command.append("--follow")
    for option, value in (
        ("since", since),
        ("until", until),
        ("after-cursor", after_cursor),
    ):
        if value is not None:
            if not value or any(character in value for character in "\0\r\n"):
                msg = f"invalid journal {option}"
                raise ValueError(msg)
            command.append(f"--{option}={value}")
    process = await create_subprocess_exec(
        *command,
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        limit=_RECORD_LIMIT,
    )
    stdout = cast(asyncio.StreamReader, process.stdout)
    diagnostics = asyncio.create_task(
        _stderr(cast(asyncio.StreamReader, process.stderr))
    )
    try:
        while line := await stdout.readline():
            yield _record(line)
        code = await process.wait()
        detail = (await diagnostics).decode(errors="replace").strip()
        if code:
            msg = f"journalctl exited with status {code}: {detail}"
            raise RuntimeError(msg)
    finally:
        await complete(_close(process, diagnostics))
