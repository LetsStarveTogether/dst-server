import os
import re
from collections.abc import Mapping
from pathlib import Path
from typing import Annotated, Literal, Self, cast
from urllib.parse import unquote, urlsplit

import grpc
from google.protobuf.message import DecodeError
from google.rpc.error_details_pb2 import RetryInfo
from opentelemetry._logs import (  # ruff: ignore[import-private-name]
    LogRecord,
    SeverityNumber,
)
from opentelemetry.exporter.otlp.proto.common._log_encoder import (  # ruff: ignore[import-private-name]
    encode_logs,
)
from opentelemetry.proto.collector.logs.v1.logs_service_pb2 import (
    ExportLogsServiceRequest,
    ExportLogsServiceResponse,
)
from opentelemetry.proto.collector.logs.v1.logs_service_pb2_grpc import LogsServiceStub
from opentelemetry.sdk._logs import (  # ruff: ignore[import-private-name]
    ReadableLogRecord,
)
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.util.instrumentation import InstrumentationScope
from opentelemetry.util.types import AttributeValue
from pydantic import ConfigDict, Field, JsonValue, field_validator, model_validator
from ulid import ULID

from dst_server.models.base import FrozenModel

from .outbox import PendingLog

MAX_EXPORT_BYTES = 1024 * 1024
NANOSECONDS = 1_000_000_000
_SCOPE = InstrumentationScope("dst-server")
RETRYABLE_CODES = frozenset({
    grpc.StatusCode.CANCELLED,
    grpc.StatusCode.DEADLINE_EXCEEDED,
    grpc.StatusCode.ABORTED,
    grpc.StatusCode.OUT_OF_RANGE,
    grpc.StatusCode.UNAVAILABLE,
    grpc.StatusCode.DATA_LOSS,
})


class OTLPSettings(FrozenModel):
    model_config = ConfigDict(hide_input_in_errors=True)

    endpoint: Annotated[str, Field(min_length=1)] = "http://localhost:4317"
    insecure: bool | None = None
    headers: tuple[tuple[str, str], ...] = Field(default=(), repr=False)
    timeout: Annotated[float, Field(gt=0)] = 10.0
    compression: Literal["none", "gzip", "deflate"] = "none"
    certificate: Path | None = None
    client_key: Path | None = None
    client_certificate: Path | None = None

    @field_validator("endpoint")
    @classmethod
    def _endpoint(cls, value: str) -> str:
        parsed = urlsplit(value if "://" in value else f"//{value}")
        if (
            value != value.strip()
            or parsed.scheme not in {"", "http", "https"}
            or not parsed.hostname
        ):
            message = "OTLP endpoint must identify an HTTP or HTTPS gRPC target"
            raise ValueError(message)
        if (
            parsed.username is not None
            or parsed.password is not None
            or parsed.query
            or parsed.fragment
            or parsed.path not in {"", "/"}
        ):
            message = "OTLP gRPC endpoint cannot have credentials, query or path"
            raise ValueError(message)
        # Parsing the port also rejects malformed and out-of-range ports.
        _ = parsed.port
        return value

    @field_validator("insecure", mode="before")
    @classmethod
    def _insecure(cls, value: object) -> object:
        if isinstance(value, str):
            if value.casefold() not in {"true", "false"}:
                message = "OTLP insecure must be 'true' or 'false'"
                raise ValueError(message)
            return value.casefold() == "true"
        return value

    @field_validator("headers", mode="before")
    @classmethod
    def _parse_headers(cls, value: object) -> object:
        if not isinstance(value, str):
            return value
        headers = {}
        for item in value.split(","):
            if not item.strip():
                continue
            name, separator, content = item.partition("=")
            if not separator:
                message = "OTLP headers must be comma-separated name=value pairs"
                raise ValueError(message)
            headers[unquote(name.strip()).lower()] = unquote(content.strip())
        return tuple(headers.items())

    @field_validator("headers")
    @classmethod
    def _validate_headers(
        cls, values: tuple[tuple[str, str], ...]
    ) -> tuple[tuple[str, str], ...]:
        for name, value in values:
            if (
                not re.fullmatch(r"[0-9a-z_.-]+", name)
                or name.endswith("-bin")
                or not re.fullmatch(r"[ -~]*", value)
            ):
                message = "OTLP headers must be valid ASCII gRPC metadata"
                raise ValueError(message)
        return values

    @model_validator(mode="after")
    def _client_credentials(self) -> Self:
        if (self.client_key is None) != (self.client_certificate is None):
            message = "OTLP client key and certificate must be configured together"
            raise ValueError(message)
        return self

    @property
    def target(self) -> str:
        return urlsplit(
            self.endpoint if "://" in self.endpoint else f"//{self.endpoint}"
        ).netloc

    @property
    def use_insecure(self) -> bool:
        scheme = urlsplit(self.endpoint).scheme
        if scheme == "https":
            return False
        return self.insecure if self.insecure is not None else scheme == "http"

    @classmethod
    def from_environment(cls) -> Self:
        values: dict[str, object] = {}
        for name in cls.model_fields:
            generic = f"OTEL_EXPORTER_OTLP_{name.upper()}"
            signal = f"OTEL_EXPORTER_OTLP_LOGS_{name.upper()}"
            if (value := os.environ.get(signal, os.environ.get(generic))) is not None:
                values[name] = value
        return cls.model_validate(values, strict=False)


class LogsExporter:
    def __init__(self, settings: OTLPSettings | None = None) -> None:
        self.settings = (
            settings if settings is not None else OTLPSettings.from_environment()
        )
        settings = self.settings
        options = (("grpc.max_receive_message_length", 4 * 1024 * 1024),)
        compression = {
            "none": grpc.Compression.NoCompression,
            "gzip": grpc.Compression.Gzip,
            "deflate": grpc.Compression.Deflate,
        }[settings.compression]
        if settings.use_insecure:
            self._channel = grpc.aio.insecure_channel(
                settings.target, options=options, compression=compression
            )
        else:
            credentials = grpc.ssl_channel_credentials(
                root_certificates=settings.certificate.read_bytes()
                if settings.certificate
                else None,
                private_key=settings.client_key.read_bytes()
                if settings.client_key
                else None,
                certificate_chain=settings.client_certificate.read_bytes()
                if settings.client_certificate
                else None,
            )
            self._channel = grpc.aio.secure_channel(
                settings.target, credentials, options=options, compression=compression
            )
        self._client = LogsServiceStub(self._channel)

    async def export(self, payload: bytes) -> ExportLogsServiceResponse:
        return await self._client.Export(
            ExportLogsServiceRequest.FromString(payload),
            metadata=self.settings.headers,
            timeout=self.settings.timeout,
        )

    async def aclose(self) -> None:
        await self._channel.close()


def retry_after(error: grpc.RpcError) -> float | None:
    call = cast("grpc.Call", error)
    for key, value in call.trailing_metadata() or ():
        if key != "google.rpc.retryinfo-bin" or not isinstance(value, bytes):
            continue
        try:
            delay = RetryInfo.FromString(value).retry_delay
        except DecodeError:
            return None
        if delay.seconds < 0 or not 0 <= delay.nanos < NANOSECONDS:
            return None
        return delay.seconds + delay.nanos / NANOSECONDS
    return None


def encode_log(
    resource: Resource,
    *,
    event_name: str,
    body: Mapping[str, JsonValue],
    observed_timestamp_ns: int,
    severity_text: str,
    attributes: Mapping[str, AttributeValue] | None = None,
) -> bytes:
    values = dict(attributes or {})
    values.setdefault("log.record.uid", str(ULID()))
    severity_text = severity_text.upper()
    try:
        severity_number = SeverityNumber[severity_text]
    except KeyError:
        message = "unknown OpenTelemetry severity"
        raise ValueError(message) from None
    record = LogRecord(
        timestamp=observed_timestamp_ns,
        observed_timestamp=observed_timestamp_ns,
        severity_text=severity_text,
        severity_number=severity_number,
        event_name=event_name,
        body=body,
        attributes=values,
    )
    payload = encode_logs((
        ReadableLogRecord(record, resource, _SCOPE),
    )).SerializeToString()
    if len(payload) > MAX_EXPORT_BYTES:
        message = "OpenTelemetry record exceeds the export size limit"
        raise ValueError(message)
    return payload


def batch(
    rows: tuple[PendingLog, ...],
) -> tuple[tuple[int, ...], bytes, tuple[int, ...]]:
    selected: list[PendingLog] = []
    invalid: list[int] = []
    size = 0
    request = ExportLogsServiceRequest()
    for row in rows:
        if selected and size + len(row.payload) > MAX_EXPORT_BYTES:
            break
        try:
            decoded = ExportLogsServiceRequest.FromString(row.payload)
        except DecodeError:
            invalid.append(row.id)
            continue
        if len(row.payload) > MAX_EXPORT_BYTES or not any(
            scope.log_records
            for resource in decoded.resource_logs
            for scope in resource.scope_logs
        ):
            invalid.append(row.id)
            continue
        selected.append(row)
        size += len(row.payload)
        request.MergeFrom(decoded)
    identities = tuple(row.id for row in selected)
    payload = selected[0].payload if len(selected) == 1 else request.SerializeToString()
    return identities, payload, tuple(invalid)
