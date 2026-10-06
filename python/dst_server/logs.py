"""Typed local log queries backed by the native journal and Netdata readers."""

from collections.abc import AsyncGenerator, Sequence
from contextlib import asynccontextmanager
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
from math import ceil, floor
from os import PathLike
from typing import Any, Literal, Self

lazy from . import _native
lazy from ._native import JournalCursorError, LogProcessError

__all__ = [
    "JournalCursorError",
    "JournalLogs",
    "JournalQuery",
    "JournalRecord",
    "JournalResult",
    "JournalStream",
    "LogProcessError",
    "NetdataLogQuery",
    "NetdataLogRecord",
    "NetdataLogResult",
    "NetdataLogs",
]


def _utc(value: datetime) -> datetime:
    if value.utcoffset() is None:
        message = "log query datetimes must include a timezone"
        raise ValueError(message)
    return value.astimezone(UTC)


@dataclass(frozen=True, slots=True)
class JournalQuery:
    """Queries default to 100 recent records. Following starts at the live tail."""

    limit: int | None = None
    direction: Literal["forward", "backward"] | None = None
    cursor: str | None = None
    since: str | datetime | None = None
    until: str | datetime | None = None
    namespace: str | None = None
    grep: str | None = None

    def to_native(self) -> dict[str, Any]:
        value = asdict(self)
        if (
            isinstance(self.since, datetime)
            and isinstance(self.until, datetime)
            and _utc(self.until) < _utc(self.since)
        ):
            message = "journal query until must not precede since"
            raise ValueError(message)
        for name in ("since", "until"):
            if isinstance(value[name], datetime):
                value[name] = _utc(value[name]).strftime("%Y-%m-%d %H:%M:%S.%f UTC")
        return {key: item for key, item in value.items() if item is not None}


@dataclass(frozen=True, slots=True)
class JournalRecord:
    fields: dict[str, Any]
    cursor: str
    timestamp: datetime
    unit: str
    message: str


def _journal_record(value: dict[str, Any]) -> JournalRecord:
    value["timestamp"] = datetime(1970, 1, 1, tzinfo=UTC) + timedelta(
        microseconds=value.pop("timestamp_us")
    )
    return JournalRecord(**value)


@dataclass(frozen=True, slots=True)
class JournalResult:
    records: tuple[JournalRecord, ...]
    next_cursor: str | None
    has_more: bool
    diagnostics: str
    diagnostics_truncated: bool


def _journal_result(value: dict[str, Any]) -> JournalResult:
    value["records"] = tuple(map(_journal_record, value["records"]))
    return JournalResult(**value)


class JournalStream:
    """Cancelling a pending read closes its native reader.

    Await close() to confirm process cleanup and finalize diagnostics.
    Process failures and incomplete output drainage can make close() raise.
    """

    __slots__ = ("_stream",)

    def __init__(self, stream: _native.JournalStream) -> None:
        self._stream = stream

    @property
    def pid(self) -> int:
        return self._stream.pid

    @property
    def diagnostics(self) -> str:
        return self._stream.diagnostics

    @property
    def diagnostics_truncated(self) -> bool:
        return self._stream.diagnostics_truncated

    def __aiter__(self) -> Self:
        return self

    async def __anext__(self) -> JournalRecord:
        record = await self._stream.next()
        if record is None:
            raise StopAsyncIteration
        return _journal_record(record)

    async def close(self) -> None:
        await self._stream.close()

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *_: object) -> None:
        await self.close()


class JournalLogs:
    __slots__ = ("_reader",)

    def __init__(
        self,
        executable: str | PathLike[str] = "journalctl",
        *,
        user: bool = False,
        max_record_bytes: int = 4 * 1024 * 1024,
        max_output_bytes: int = 64 * 1024 * 1024,
    ) -> None:
        self._reader = _native.JournalLogs(
            executable, max_record_bytes, max_output_bytes, user
        )

    async def query(
        self,
        units: Sequence[str] | None,
        request: JournalQuery | None = None,
        *,
        completion_timeout: float = 120.0,
    ) -> JournalResult:
        result = await self._reader.query(
            units, (request or JournalQuery()).to_native(), completion_timeout
        )
        return _journal_result(result)

    @asynccontextmanager
    async def follow(
        self, units: Sequence[str] | None, request: JournalQuery | None = None
    ) -> AsyncGenerator[JournalStream]:
        async with JournalStream(
            await self._reader.follow(
                units,
                (request or JournalQuery()).to_native(),
            )
        ) as stream:
            yield stream


@dataclass(frozen=True, slots=True)
class NetdataLogQuery:
    """Query whole seconds, rounding fractional datetime bounds outwards."""

    since: datetime | int
    until: datetime | int | None = None
    service_name: str | None = None
    service_namespace: str | None = None
    filters: tuple[tuple[str, str], ...] = ()
    query: str | None = None
    userid: str | None = None
    fields: tuple[str, ...] = ()
    limit: int = 200

    def to_native(self) -> dict[str, Any]:
        value = asdict(self)
        since, until = self.since, self.until
        if isinstance(since, datetime):
            since = _utc(since).timestamp()
            value["since"] = floor(since)
        if isinstance(until, datetime):
            until = _utc(until).timestamp()
            value["until"] = ceil(until)
        if until is not None and until <= since:
            message = "Netdata query until must be later than since"
            raise ValueError(message)
        return value


@dataclass(frozen=True, slots=True)
class NetdataLogRecord:
    timestamp_ns: int
    fields: tuple[tuple[str, str], ...]

    def values(self, key: str) -> tuple[str, ...]:
        return tuple(value for field, value in self.fields if field == key)


@dataclass(frozen=True, slots=True)
class NetdataLogResult:
    records: tuple[NetdataLogRecord, ...]
    matched: int | None
    since: datetime
    until: datetime
    diagnostics: str
    diagnostics_truncated: bool
    truncated: bool | None


class NetdataLogs:
    __slots__ = ("_reader",)

    def __init__(
        self,
        executable: str | PathLike[str] = "/usr/lib/netdata/plugins.d/otel-plugin",
        *,
        stock_config: str | PathLike[str] = "/usr/lib/netdata/conf.d/otel.yaml",
        config: str | PathLike[str] = "/etc/netdata/otel.yaml",
        max_concurrency: int = 1,
        max_record_bytes: int = 4 * 1024 * 1024,
        max_output_bytes: int = 64 * 1024 * 1024,
    ) -> None:
        self._reader = _native.NetdataLogs(
            executable,
            stock_config,
            config,
            max_concurrency,
            max_record_bytes,
            max_output_bytes,
        )

    async def query(
        self, request: NetdataLogQuery, *, completion_timeout: float = 120.0
    ) -> NetdataLogResult:
        result = await self._reader.query(request.to_native(), completion_timeout)
        result["records"] = tuple(
            NetdataLogRecord(
                timestamp_ns=record["timestamp_ns"],
                fields=tuple(tuple(pair) for pair in record["fields"]),
            )
            for record in result["records"]
        )
        result["since"] = datetime.fromtimestamp(result["since"], UTC)
        result["until"] = datetime.fromtimestamp(result["until"], UTC)
        return NetdataLogResult(**result)
