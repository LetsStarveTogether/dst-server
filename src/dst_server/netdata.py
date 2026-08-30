import asyncio
import math
import os
import subprocess  # ruff: ignore[suspicious-subprocess-import]
from contextlib import suppress
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated, Self

from pydantic import AwareDatetime, Field, field_validator, model_validator

from dst_server.models.base import FrozenModel, PositiveInt

type NetdataLogFilter = tuple[
    Annotated[str, Field(min_length=1)],
    str,
]


class NetdataLogQuery(FrozenModel):
    since: AwareDatetime
    until: AwareDatetime | None = None
    service_name: Annotated[str, Field(min_length=1)] | None = "dst-server"
    service_namespace: Annotated[str, Field(min_length=1)] | None = None
    filters: tuple[NetdataLogFilter, ...] = ()
    query: Annotated[str, Field(min_length=1)] | None = None
    fields: tuple[Annotated[str, Field(min_length=1)], ...] = ()
    limit: PositiveInt = 200

    @field_validator("since", "until", mode="after")
    @classmethod
    def _normalize_time(cls, value: datetime | None) -> datetime | None:
        if value is None:
            return None
        timestamp = value.timestamp()
        if not 0 <= timestamp <= (1 << 32) - 1:
            msg = "Netdata log query time must fit unsigned 32-bit Unix seconds"
            raise ValueError(msg)
        return datetime.fromtimestamp(int(timestamp), UTC)

    @model_validator(mode="after")
    def _validate_query(self) -> Self:
        if self.until is not None and self.until <= self.since:
            msg = "Netdata log query until must be later than since"
            raise ValueError(msg)
        if self.service_namespace is not None and self.service_name is None:
            msg = "Netdata service namespace requires a service name"
            raise ValueError(msg)
        return self


class NetdataLogRecord(FrozenModel):
    timestamp_ns: int
    fields: tuple[tuple[str, str], ...]


class NetdataLogResult(FrozenModel):
    records: tuple[NetdataLogRecord, ...]
    diagnostics: str = ""


class NetdataLogs:
    def __init__(
        self,
        executable: str | Path = "/usr/lib/netdata/plugins.d/otel-plugin",
        *,
        stock_config: str | Path = "/usr/lib/netdata/conf.d/otel.yaml",
        config: str | Path = "/etc/netdata/otel.yaml",
        max_concurrency: int = 1,
    ) -> None:
        self.executable = _path("Netdata executable", executable)
        self.stock_config = _path("Netdata stock config", stock_config)
        self.config = _path("Netdata config", config)
        if (
            isinstance(max_concurrency, bool)
            or not isinstance(max_concurrency, int)
            or max_concurrency < 1
        ):
            msg = "Netdata query concurrency must be a positive integer"
            raise ValueError(msg)
        self._semaphore = asyncio.Semaphore(max_concurrency)

    async def query(
        self,
        request: NetdataLogQuery,
        *,
        completion_timeout: float = 30,
    ) -> NetdataLogResult:
        if (
            isinstance(completion_timeout, bool)
            or not isinstance(completion_timeout, (int, float))
            or not math.isfinite(completion_timeout)
            or completion_timeout <= 0
        ):
            msg = "Netdata query timeout must be a positive finite number"
            raise ValueError(msg)
        command = self._command(request)
        async with self._semaphore:
            process = await asyncio.create_subprocess_exec(
                *command,
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            try:
                async with asyncio.timeout(completion_timeout):
                    stdout, stderr = await process.communicate()
            except BaseException:
                if process.returncode is None:
                    with suppress(ProcessLookupError):
                        process.kill()
                await process.wait()
                raise
        if process.returncode:
            raise subprocess.CalledProcessError(
                process.returncode,
                command,
                stdout,
                stderr,
            )
        records: list[NetdataLogRecord] = []
        for number, line in enumerate(stdout.splitlines(), 1):
            try:
                records.append(NetdataLogRecord.model_validate_json(line))
            except ValueError as error:
                msg = f"invalid Netdata NDJSON record on line {number}"
                raise ValueError(msg) from error
        return NetdataLogResult(
            records=tuple(records),
            diagnostics=stderr.decode(errors="replace"),
        )

    def _command(self, request: NetdataLogQuery) -> tuple[str, ...]:
        command = [
            self.executable,
            "logs",
            "--stock-config",
            self.stock_config,
            "--config",
            self.config,
            "--since",
            str(int(request.since.timestamp())),
        ]
        if request.until is not None:
            command.extend(("--until", str(int(request.until.timestamp()))))
        if request.service_name is not None:
            command.extend(("--name", request.service_name))
        if request.service_namespace is not None:
            command.extend(("--namespace", request.service_namespace))
        if request.filters:
            command.extend(("--filter", _filters(request.filters)))
        if request.query is not None:
            command.extend(("--query", request.query))
        if request.fields:
            command.extend(("--fields", _joined("field", request.fields)))
        command.extend(("--limit", str(request.limit), "--output", "ndjson"))
        return tuple(command)


def _path(name: str, value: str | Path) -> str:
    result = os.fspath(value)
    if not result:
        msg = f"{name} must not be empty"
        raise ValueError(msg)
    return result


def _filters(values: tuple[NetdataLogFilter, ...]) -> str:
    terms = []
    for field, value in values:
        if field != field.strip() or value != value.strip():
            msg = "Netdata filter fields and values cannot have surrounding whitespace"
            raise ValueError(msg)
        if any(character in field for character in ",=~"):
            msg = f"Netdata filter field cannot contain a comma, '=' or '~': {field!r}"
            raise ValueError(msg)
        if "," in value:
            msg = f"Netdata CLI cannot encode a comma in filter value: {value!r}"
            raise ValueError(msg)
        terms.append(f"{field}={value}")
    return ",".join(terms)


def _joined(name: str, values: tuple[str, ...]) -> str:
    if any(value != value.strip() for value in values):
        msg = f"Netdata {name} cannot have surrounding whitespace"
        raise ValueError(msg)
    if any("," in value for value in values):
        msg = f"Netdata {name} cannot contain a comma"
        raise ValueError(msg)
    return ",".join(values)
