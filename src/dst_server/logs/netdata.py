"""Bounded, offline queries against Netdata's local OpenTelemetry log store."""

import asyncio
import os
import re
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Annotated, Self

from pydantic import (
    AwareDatetime,
    Field,
    TypeAdapter,
    computed_field,
    field_validator,
    model_validator,
)

from dst_server.models.base import FrozenModel, NonNegativeInt, PositiveInt
from dst_server.timeouts import DEFAULT_COMMAND_TIMEOUT, positive_timeout

from ._process import log_process, positive_bytes

type NetdataLogFilter = tuple[Annotated[str, Field(min_length=1)], str]

_POSITIVE_INT = TypeAdapter(PositiveInt)
_SUMMARY = re.compile(r"matched=([0-9]+) returned=([0-9]+) window=([0-9]+)\.\.([0-9]+)")


class NetdataLogQuery(FrozenModel):
    """Select the newest limited records in a whole-second [since, until) window.

    A service name selects one stream: an omitted or empty namespace selects
    the empty namespace, not every namespace with that name.
    Filters OR repeated fields and AND different fields.
    ``query`` is Netdata's unanchored regex over ``key=value`` pairs.
    """

    since: AwareDatetime
    until: AwareDatetime | None = None
    service_name: Annotated[str, Field(min_length=1)] | None = None
    service_namespace: str | None = None
    filters: tuple[NetdataLogFilter, ...] = ()
    query: Annotated[str, Field(min_length=1)] | None = None
    fields: tuple[Annotated[str, Field(min_length=1)], ...] = ()
    limit: PositiveInt = 200

    @field_validator("since", "until")
    @classmethod
    def _seconds(cls, value: datetime | None) -> datetime | None:
        if value is None:
            return None
        value = value.astimezone(UTC).replace(microsecond=0)
        if not 0 <= value.timestamp() <= (1 << 32) - 1:
            msg = "Netdata query time must fit unsigned 32-bit Unix seconds"
            raise ValueError(msg)
        return value

    @field_validator("service_name", "service_namespace", "query")
    @classmethod
    def _argument(cls, value: str | None) -> str | None:
        if value is not None and "\0" in value:
            msg = "Netdata arguments cannot contain NUL"
            raise ValueError(msg)
        return value

    @field_validator("filters")
    @classmethod
    def _filters(
        cls, values: tuple[NetdataLogFilter, ...]
    ) -> tuple[NetdataLogFilter, ...]:
        for field, value in values:
            if field != field.strip() or value != value.strip():
                msg = "Netdata filters cannot encode surrounding whitespace"
                raise ValueError(msg)
            if any(character in field for character in ",=~\0"):
                msg = "Netdata filter fields cannot contain comma, '=', '~' or NUL"
                raise ValueError(msg)
            if "," in value or "\0" in value:
                msg = "Netdata filter values cannot contain comma or NUL"
                raise ValueError(msg)
        return values

    @field_validator("fields")
    @classmethod
    def _fields(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        for value in values:
            if value != value.strip():
                msg = "Netdata fields cannot encode surrounding whitespace"
                raise ValueError(msg)
            if "," in value or "\0" in value:
                msg = "Netdata fields cannot contain comma or NUL"
                raise ValueError(msg)
        return values

    @model_validator(mode="after")
    def _window_and_stream(self) -> Self:
        if self.until is not None and self.until <= self.since:
            msg = "Netdata query until must be later than since"
            raise ValueError(msg)
        if self.service_namespace is not None and self.service_name is None:
            msg = "Netdata service namespace requires a service name"
            raise ValueError(msg)
        return self


class NetdataLogRecord(FrozenModel):
    timestamp_ns: Annotated[int, Field(ge=0, le=(1 << 64) - 1)]
    fields: tuple[tuple[str, str], ...]

    def values(self, key: str) -> tuple[str, ...]:
        return tuple(value for field, value in self.fields if field == key)


class NetdataLogResult(FrozenModel):
    """The CLI's local view, which may omit active writes or unreadable files.

    ``matched`` is the backend's reported count, not a completeness guarantee.
    Diagnostics can report source failures even after a successful query.
    """

    records: tuple[NetdataLogRecord, ...]
    matched: NonNegativeInt | None
    since: AwareDatetime
    until: AwareDatetime
    diagnostics: str
    diagnostics_truncated: bool

    @computed_field
    @property
    def truncated(self) -> bool | None:
        return None if self.matched is None else self.matched > len(self.records)


class NetdataLogs:
    def __init__(
        self,
        executable: str | Path = "/usr/lib/netdata/plugins.d/otel-plugin",
        *,
        stock_config: str | Path = "/usr/lib/netdata/conf.d/otel.yaml",
        config: str | Path = "/etc/netdata/otel.yaml",
        max_concurrency: int = 1,
        max_record_bytes: int = 4 * 1024 * 1024,
        max_output_bytes: int = 64 * 1024 * 1024,
    ) -> None:
        self.executable = _path(executable)
        self.stock_config = _path(stock_config)
        self.config = _path(config)
        self.max_record_bytes = positive_bytes(max_record_bytes)
        self.max_output_bytes = positive_bytes(max_output_bytes)
        self._semaphore = asyncio.Semaphore(
            _POSITIVE_INT.validate_python(max_concurrency, strict=True)
        )

    async def query(
        self,
        request: NetdataLogQuery,
        *,
        completion_timeout: float = DEFAULT_COMMAND_TIMEOUT,
    ) -> NetdataLogResult:
        timeout = positive_timeout(completion_timeout)
        # Capture before waiting for a slot; include the current second.
        until = request.until or datetime.now(UTC).replace(microsecond=0) + timedelta(
            seconds=1
        )
        effective = request.replace(until=until)
        command = self._command(effective)
        records: list[NetdataLogRecord] = []
        async with asyncio.timeout(timeout), self._semaphore:
            async with log_process(
                command,
                max_record_bytes=self.max_record_bytes,
                max_output_bytes=self.max_output_bytes,
            ) as output:
                async for line in output:
                    if len(records) == request.limit:
                        msg = "Netdata returned more records than the requested limit"
                        raise ValueError(msg)
                    try:
                        records.append(NetdataLogRecord.model_validate_json(line))
                    except ValueError as error:
                        msg = (
                            f"invalid Netdata NDJSON record on line {len(records) + 1}"
                        )
                        raise ValueError(msg) from error
            matched = _matched(
                effective,
                len(records),
                output.diagnostics,
                output.diagnostics_truncated,
            )
        return NetdataLogResult(
            records=tuple(records),
            matched=matched,
            since=effective.since,
            until=until,
            diagnostics=output.diagnostics,
            diagnostics_truncated=output.diagnostics_truncated,
        )

    def _command(self, request: NetdataLogQuery) -> tuple[str, ...]:
        command = [
            self.executable,
            "logs",
            f"--stock-config={self.stock_config}",
            f"--config={self.config}",
            f"--since={int(request.since.timestamp())}",
        ]
        if request.until is not None:
            command.append(f"--until={int(request.until.timestamp())}")
        if request.service_name is not None:
            command.append(f"--name={request.service_name}")
        if request.service_namespace is not None:
            command.append(f"--namespace={request.service_namespace}")
        if request.filters:
            command.append(
                "--filter="
                + ",".join(f"{key}={value}" for key, value in request.filters)
            )
        if request.query is not None:
            command.append(f"--query={request.query}")
        if request.fields:
            command.append("--fields=" + ",".join(request.fields))
        command.extend((f"--limit={request.limit}", "--output=ndjson"))
        return tuple(command)


def _matched(
    request: NetdataLogQuery,
    returned: int,
    diagnostics: str,
    diagnostics_truncated: bool,
) -> int | None:
    summaries = [
        match
        for line in diagnostics.splitlines()
        if (match := _SUMMARY.fullmatch(line))
    ]
    if len(summaries) == 1:
        matched, count, since, until = map(int, summaries[0].groups())
        if count != returned:
            msg = "Netdata summary returned count does not match its records"
            raise ValueError(msg)
        if (
            since != int(request.since.timestamp())
            or request.until is None
            or until != int(request.until.timestamp())
        ):
            msg = "Netdata summary window does not match the query"
            raise ValueError(msg)
        return matched
    if len(summaries) > 1:
        msg = "Netdata returned multiple query summaries"
        raise ValueError(msg)
    if request.until is not None:
        stream = (
            f", stream={request.service_namespace or ''}/{request.service_name}"
            if request.service_name is not None
            else ""
        )
        empty = (
            "no WAL/SFST files matched "
            f"(tenant=default, window={int(request.since.timestamp())}.."
            f"{int(request.until.timestamp())}{stream})"
        )
        if not returned and diagnostics.rstrip().endswith(empty):
            return 0
    if diagnostics_truncated:
        return None
    msg = "Netdata did not return a valid query summary"
    raise ValueError(msg)


def _path(value: str | Path) -> str:
    result = os.fspath(value)
    if not result or "\0" in result:
        msg = "Netdata paths must be nonempty and cannot contain NUL"
        raise ValueError(msg)
    return result
