"""Retained journal queries and owned live readers on the local host."""

import asyncio
import re
from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from typing import Literal, Self, cast

import orjson
from pydantic import (
    AwareDatetime,
    JsonValue,
    computed_field,
    field_validator,
    model_validator,
)

from dst_server.models.base import FrozenModel, NonNegativeInt
from dst_server.timeouts import DEFAULT_COMMAND_TIMEOUT, positive_timeout

from ._process import LogOutput, log_process, positive_bytes

_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)
_UNIT_PATTERN = re.compile(r"(?:[A-Za-z0-9:_.@*?\[\]-]|\\x[0-9a-f]{2})+\Z")
_MAX_UNIT_BYTES = 255


def _text(value: JsonValue) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        if all(type(item) is int for item in value):
            return bytes(cast(list[int], value)).decode(errors="replace")
        return "\n".join(_text(item) for item in value)
    message = "invalid journal field value"
    raise ValueError(message)


class JournalRecord(FrozenModel):
    fields: dict[str, JsonValue]

    @model_validator(mode="after")
    def _validate_record(self) -> Self:
        for value in self.fields.values():
            _text(value)
        cursor = self.fields.get("__CURSOR")
        timestamp = self.fields.get("__REALTIME_TIMESTAMP")
        if (
            not isinstance(cursor, str)
            or not cursor
            or any(character in cursor for character in "\0\r\n")
            or not isinstance(timestamp, str)
            or re.fullmatch(r"[0-9]+", timestamp) is None
        ):
            message = "invalid journal record metadata"
            raise ValueError(message)
        try:
            _ = self.timestamp
        except OverflowError as error:
            message = "journal timestamp is outside the supported datetime range"
            raise ValueError(message) from error
        return self

    @computed_field
    @property
    def cursor(self) -> str:
        return _text(self.fields["__CURSOR"])

    @computed_field
    @property
    def timestamp(self) -> AwareDatetime:
        return _EPOCH + timedelta(
            microseconds=int(_text(self.fields["__REALTIME_TIMESTAMP"]))
        )

    @computed_field
    @property
    def unit(self) -> str:
        return _text(
            self.fields.get("UNIT")
            or self.fields.get("_SYSTEMD_UNIT")
            or self.fields.get("_SYSTEMD_USER_UNIT")
        )

    @computed_field
    @property
    def message(self) -> str:
        return _text(self.fields.get("MESSAGE"))


class JournalQuery(FrozenModel):
    limit: NonNegativeInt = 100
    direction: Literal["forward", "backward"] = "backward"
    cursor: str | None = None
    since: str | AwareDatetime | None = None
    until: str | AwareDatetime | None = None
    namespace: str | None = None

    @field_validator("cursor", "since", "until", "namespace")
    @classmethod
    def _validate_text(cls, value: str | datetime | None) -> str | datetime | None:
        if isinstance(value, str) and (
            not value.strip() or any(character in value for character in "\0\r\n")
        ):
            message = "journal query values cannot be empty or contain NUL or newline"
            raise ValueError(message)
        return value

    @model_validator(mode="after")
    def _validate_dates(self) -> Self:
        if self.cursor is not None and self.since is not None:
            message = "journal cursor and since cannot be combined"
            raise ValueError(message)
        if (
            isinstance(self.since, datetime)
            and isinstance(self.until, datetime)
            and self.until < self.since
        ):
            message = "journal query until must not precede since"
            raise ValueError(message)
        return self


class JournalResult(FrozenModel):
    records: tuple[JournalRecord, ...]
    next_cursor: str | None
    has_more: bool
    diagnostics: str
    diagnostics_truncated: bool


class JournalCursorError(RuntimeError):
    def __init__(self, cursor: str) -> None:
        self.cursor = cursor
        super().__init__("journal cursor is unavailable for the selected query")


class JournalStream:
    """Records and bounded diagnostics, valid until the enclosing context exits."""

    def __init__(self, output: LogOutput, cursor: str | None) -> None:
        self._output = output
        self._cursor = cursor

    @property
    def diagnostics(self) -> str:
        return self._output.diagnostics

    @property
    def diagnostics_truncated(self) -> bool:
        return self._output.diagnostics_truncated

    def __aiter__(self) -> Self:
        return self

    async def __anext__(self) -> JournalRecord:
        if self._cursor is not None:
            cursor = self._cursor
            try:
                anchor = _record(await anext(self._output))
            except StopAsyncIteration as error:
                raise JournalCursorError(cursor) from error
            if anchor.cursor != cursor:
                raise JournalCursorError(cursor)
            self._cursor = None
        return _record(await anext(self._output))


def _record(line: bytes) -> JournalRecord:
    try:
        return JournalRecord(fields=orjson.loads(line))
    except (UnicodeDecodeError, ValueError) as error:
        message = "invalid journalctl JSON record"
        raise ValueError(message) from error


def _command(units: Sequence[str], request: JournalQuery, *, follow: bool) -> list[str]:
    if isinstance(units, str) or not units:
        message = "journal logs require at least one explicit unit pattern"
        raise ValueError(message)
    for unit in units:
        if (
            not isinstance(unit, str)
            or _UNIT_PATTERN.fullmatch(unit) is None
            or unit.startswith(("-", "."))
            or len(unit.encode()) > _MAX_UNIT_BYTES
            or "\\x00" in unit
        ):
            message = "invalid journal unit pattern"
            raise ValueError(message)
    command = ["journalctl", "--no-pager", "--all", "--output=json"]
    command.extend(f"--unit={unit}" for unit in dict.fromkeys(units))
    if follow:
        command.extend(("--follow", f"--lines={request.limit}"))
    else:
        count = request.limit + 1 + (request.cursor is not None)
        command.append(
            f"--lines={'+' if request.direction == 'forward' else ''}{count}"
        )
        if request.direction == "backward":
            command.append("--reverse")
    for name in ("cursor", "since", "until", "namespace"):
        value = getattr(request, name)
        if value is not None:
            if isinstance(value, datetime):
                value = value.astimezone(UTC).strftime("%Y-%m-%d %H:%M:%S.%f UTC")
            command.append(f"--{name}={value}")
    return command


_DEFAULT_QUERY = JournalQuery()
_DEFAULT_FOLLOW = JournalQuery(direction="forward", limit=0)


class JournalLogs:
    def __init__(
        self,
        *,
        max_record_bytes: int = 4 * 1024 * 1024,
        max_output_bytes: int = 64 * 1024 * 1024,
    ) -> None:
        self.max_record_bytes = positive_bytes(max_record_bytes)
        self.max_output_bytes = positive_bytes(max_output_bytes)

    async def query(
        self,
        units: Sequence[str],
        request: JournalQuery = _DEFAULT_QUERY,
        *,
        completion_timeout: float = DEFAULT_COMMAND_TIMEOUT,
    ) -> JournalResult:
        """Return a bounded page in the requested journal order.

        Forward reads oldest to newest; backward reads newest to oldest.
        has_more indicates an additional matching record beyond this page.
        next_cursor identifies the last delivered record, excluding lookahead.
        Resume with request.replace(cursor=result.next_cursor, since=None);
        journalctl forbids combining since with a cursor. Keep until to retain
        the same upper time bound across pages.

        Returns:
            Records, continuation metadata, and bounded native diagnostics.
        """
        request = JournalQuery.model_validate(request)
        command = _command(units, request, follow=False)
        async with (
            asyncio.timeout(positive_timeout(completion_timeout)),
            log_process(
                command,
                max_record_bytes=self.max_record_bytes,
                max_output_bytes=self.max_output_bytes,
            ) as output,
        ):
            records = [record async for record in JournalStream(output, request.cursor)]
            returned = tuple(records[: request.limit])
            return JournalResult(
                records=returned,
                next_cursor=returned[-1].cursor if returned else None,
                has_more=len(records) > request.limit,
                diagnostics=output.diagnostics,
                diagnostics_truncated=output.diagnostics_truncated,
            )

    @asynccontextmanager
    async def follow(
        self,
        units: Sequence[str],
        request: JournalQuery = _DEFAULT_FOLLOW,
    ) -> AsyncIterator[JournalStream]:
        """Read initial history and new records with one owned journalctl process.

        Without a cursor, limit selects the recent initial history; zero starts
        with new records. With a cursor, every retained record after it is read.
        A missing cursor can only be detected when a record or EOF arrives.
        Diagnostics remain available on the stream after the context exits.
        Unit globs expand when the reader starts; new units are not added later.

        Yields:
            The owned record iterator with its native reader diagnostics.

        Raises:
            ValueError: The query direction or a query value is invalid.
        """
        request = JournalQuery.model_validate(request)
        if request.direction != "forward":
            message = "journal follow requires forward direction"
            raise ValueError(message)
        command = _command(units, request, follow=True)
        async with log_process(
            command,
            max_record_bytes=self.max_record_bytes,
            max_output_bytes=None,
        ) as output:
            yield JournalStream(output, request.cursor)
