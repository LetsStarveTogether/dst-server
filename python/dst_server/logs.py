"""Typed local log queries backed by the native journal and Netdata readers."""

from collections.abc import AsyncGenerator, Sequence
from contextlib import asynccontextmanager
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
from os import PathLike
from typing import Any, Literal, Self

from . import _native

JournalCursorError = _native.JournalCursorError
LogProcessError = _native.LogProcessError

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
    limit: int = 100
    direction: Literal["forward", "backward"] = "backward"
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
        return value


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


class JournalStream:
    """Cancelling a pending read closes its native reader."""

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
        max_record_bytes: int = 4 * 1024 * 1024,
        max_output_bytes: int = 64 * 1024 * 1024,
    ) -> None:
        self._reader = _native.JournalLogs(
            executable, max_record_bytes, max_output_bytes
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
        result["records"] = tuple(
            _journal_record(record) for record in result["records"]
        )
        return JournalResult(**result)

    @asynccontextmanager
    async def follow(
        self, units: Sequence[str] | None, request: JournalQuery | None = None
    ) -> AsyncGenerator[JournalStream]:
        stream = JournalStream(
            await self._reader.follow(
                units,
                (request or JournalQuery(limit=0, direction="forward")).to_native(),
            )
        )
        try:
            yield stream
        finally:
            await stream.close()


@dataclass(frozen=True, slots=True)
class NetdataLogQuery:
    since: datetime | int
    until: datetime | int | None = None
    service_name: str | None = None
    service_namespace: str | None = None
    filters: tuple[tuple[str, str], ...] = ()
    query: str | None = None
    fields: tuple[str, ...] = ()
    limit: int = 200

    def to_native(self) -> dict[str, Any]:
        value = asdict(self)
        for name in ("since", "until"):
            if isinstance(value[name], datetime):
                value[name] = int(_utc(value[name]).replace(microsecond=0).timestamp())
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
