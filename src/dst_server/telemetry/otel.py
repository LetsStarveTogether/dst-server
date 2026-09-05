import asyncio
import os
import random
import re
from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from pathlib import Path
from threading import Lock
from typing import TYPE_CHECKING, Protocol, cast

import grpc
from google.protobuf.message import DecodeError
from google.rpc.error_details_pb2 import RetryInfo
from opentelemetry import metrics, trace
from opentelemetry._logs import (  # ruff: ignore[import-private-name]
    LogRecord,
    SeverityNumber,
)
from opentelemetry.exporter.otlp.proto.common._log_encoder import (  # ruff: ignore[import-private-name]
    encode_logs,
)
from opentelemetry.exporter.otlp.proto.grpc._log_exporter import (  # ruff: ignore[import-private-name]
    OTLPLogExporter,
)
from opentelemetry.exporter.otlp.proto.grpc.metric_exporter import OTLPMetricExporter
from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter
from opentelemetry.proto.collector.logs.v1.logs_service_pb2 import (
    ExportLogsServiceRequest,
    ExportLogsServiceResponse,
)
from opentelemetry.sdk._logs import (  # ruff: ignore[import-private-name]
    ReadableLogRecord,
)
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from opentelemetry.sdk.util.instrumentation import InstrumentationScope
from opentelemetry.util.types import AttributeValue
from pydantic import JsonValue
from ulid import ULID

from .delivery import DeliveryStatus, Outbox, PendingLog

if TYPE_CHECKING:
    from dst_server.events import ObservedGameEvent

_configured = False
_configure_lock = Lock()
RETRY_INITIAL_SECONDS = 1.0
RETRY_MAX_SECONDS = 30.0
MAX_EXPORT_BYTES = 1024 * 1024
_SCOPE = InstrumentationScope("dst-server")
_RETRYABLE_CODES = frozenset({
    grpc.StatusCode.CANCELLED,
    grpc.StatusCode.DEADLINE_EXCEEDED,
    grpc.StatusCode.ABORTED,
    grpc.StatusCode.OUT_OF_RANGE,
    grpc.StatusCode.UNAVAILABLE,
    grpc.StatusCode.DATA_LOSS,
})


class _ShutdownResource(Protocol):
    def shutdown(self) -> object: ...


class OTLPLogSender:
    def __init__(self, *, meter_provider: MeterProvider | None = None) -> None:
        self._exporter = OTLPLogExporter(meter_provider=meter_provider)

    def export(self, payload: bytes) -> ExportLogsServiceResponse:
        # SDK 1.44 owns TLS, headers, compression and timeout configuration;
        # its export() discards partial_success, so read the generated stub response.
        exporter = self._exporter
        client = exporter._client  # ruff: ignore[private-member-access]
        if client is None:
            message = "OTLP sender has no active connection"
            raise RuntimeError(message)
        return client.Export(
            request=ExportLogsServiceRequest.FromString(payload),
            metadata=exporter._headers,  # ruff: ignore[private-member-access]
            timeout=exporter._timeout,  # ruff: ignore[private-member-access]
        )

    def shutdown(self) -> None:
        self._exporter.shutdown()


def _otlp_exporter_enabled(variable: str) -> bool:
    value = os.environ.get(variable, "").casefold()
    if not value or value == "otlp":
        return True
    if value == "none":
        return False
    message = f"{variable} must be 'otlp' or 'none'"
    raise ValueError(message)


def _shutdown_resources(resources: tuple[_ShutdownResource, ...]) -> None:
    failures: list[BaseException] = []
    for resource in resources:
        try:
            resource.shutdown()
        except BaseException as error:
            failures.append(error)
    if len(failures) == 1:
        raise failures[0]
    if failures:
        message = "failed to shut down OpenTelemetry providers"
        raise BaseExceptionGroup(message, failures)


def _retry_info(error: grpc.RpcError) -> float | None:
    call = cast("grpc.Call", error)
    for key, value in call.trailing_metadata() or ():
        if key != "google.rpc.retryinfo-bin" or not isinstance(value, bytes):
            continue
        try:
            delay = RetryInfo.FromString(value).retry_delay
        except DecodeError:
            return None
        seconds = delay.seconds + delay.nanos / 1_000_000_000
        return seconds if seconds >= 0 else None
    return None


def _batch(
    rows: tuple[PendingLog, ...],
) -> tuple[tuple[int, ...], bytes, tuple[int, ...]]:
    selected = []
    invalid = []
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
    if len(selected) == 1:
        return identities, selected[0].payload, tuple(invalid)
    return identities, request.SerializeToString(), tuple(invalid)


@dataclass(slots=True)
class Pipeline:
    resource: Resource
    outbox: Outbox | None
    sender: OTLPLogSender | None
    _resources: tuple[_ShutdownResource, ...] = field(repr=False)
    _closed: bool = field(default=False, init=False)
    _worker: asyncio.Task[None] | None = field(default=None, init=False)
    _shutdown_task: asyncio.Task[None] | None = field(default=None, init=False)
    _writes: set[asyncio.Task[int]] = field(default_factory=set, init=False)
    _wake: asyncio.Event = field(default_factory=asyncio.Event, init=False)
    _stop: asyncio.Event = field(default_factory=asyncio.Event, init=False)
    _last_error: str | None = field(default=None, init=False)

    def __post_init__(self) -> None:
        if self.logs_enabled:
            loop = asyncio.get_running_loop()
            self._worker = loop.create_task(self._deliver(), name="dst-otel-delivery")

    @property
    def logs_enabled(self) -> bool:
        return self.outbox is not None and self.sender is not None

    def status(self) -> DeliveryStatus:
        status = self.outbox.stats() if self.outbox is not None else DeliveryStatus()
        return (
            replace(status, last_error=self._last_error) if self._last_error else status
        )

    async def emit_event(
        self,
        observed: ObservedGameEvent,
        *,
        attributes: Mapping[str, AttributeValue] | None = None,
    ) -> None:
        event = observed.record
        values = dict(attributes or {})
        values.update({
            "log.record.uid": f"{event.nonce}:{event.generation}:{event.seq}",
            "dst.game.attempt.id": event.nonce,
            "dst.runtime.generation": event.generation,
            "dst.event.sequence": event.seq,
            "dst.tick": event.tick,
            "dst.monotonic_ms": event.monotonic_ms,
        })
        values.pop("dst.session.id", None)
        values.pop("dst.world.cycle", None)
        if event.session_id is not None:
            values["dst.session.id"] = event.session_id
        if event.cycle is not None:
            values["dst.world.cycle"] = event.cycle
        await self.emit_operational(
            event_name=event.event,
            body=event.data.model_dump(mode="json"),
            observed_timestamp_ns=observed.observed_timestamp_ns,
            severity_text="ERROR" if event.event == "dst.telemetry.error" else "INFO",
            attributes=values,
        )

    async def emit_operational(
        self,
        *,
        event_name: str,
        body: Mapping[str, JsonValue],
        observed_timestamp_ns: int,
        severity_text: str,
        attributes: Mapping[str, AttributeValue] | None = None,
    ) -> None:
        if self._closed:
            message = "OpenTelemetry pipeline is closed"
            raise RuntimeError(message)
        if self.outbox is None:
            return
        values = dict(attributes or {})
        values.setdefault("log.record.uid", str(ULID()))
        severity_text = severity_text.upper()
        try:
            severity_number = SeverityNumber[severity_text]
        except KeyError:
            message = "unknown OpenTelemetry severity"
            raise ValueError(message) from None
        log = LogRecord(
            timestamp=observed_timestamp_ns,
            observed_timestamp=observed_timestamp_ns,
            severity_text=severity_text,
            severity_number=severity_number,
            event_name=event_name,
            body=body,
            attributes=values,
        )
        readable = ReadableLogRecord(log, self.resource, _SCOPE)
        payload = encode_logs((readable,)).SerializeToString()
        if len(payload) > MAX_EXPORT_BYTES:
            message = "OpenTelemetry record exceeds the export size limit"
            raise ValueError(message)
        pending = asyncio.create_task(asyncio.to_thread(self.outbox.append, payload))
        self._writes.add(pending)
        pending.add_done_callback(self._written)
        await asyncio.shield(pending)

    def _written(self, task: asyncio.Task[int]) -> None:
        self._writes.discard(task)
        if not task.cancelled() and task.exception() is not None:
            self._last_error = "storage_write_failed"
        self._wake.set()

    async def _wait_retry(self, delay: float) -> None:
        try:
            async with asyncio.timeout(delay):
                await self._stop.wait()
        except TimeoutError:
            pass

    async def _send(self, payload: bytes) -> tuple[str | None, int | None] | None:
        if self.sender is None:
            return None
        delay = RETRY_INITIAL_SECONDS
        while not self._stop.is_set():
            wait = delay * random.uniform(0.8, 1.2)  # ruff: ignore[suspicious-non-cryptographic-random-usage]
            try:
                response = await asyncio.to_thread(self.sender.export, payload)
            except grpc.RpcError as error:
                code = cast("grpc.Call", error).code()
                retry_after = _retry_info(error)
                reason = f"export_{code.name.lower()}" if code else "export_unknown"
                self._last_error = reason
                if code not in _RETRYABLE_CODES and not (
                    code is grpc.StatusCode.RESOURCE_EXHAUSTED
                    and retry_after is not None
                ):
                    return reason, None
                if retry_after is not None:
                    wait = retry_after
            except Exception:
                self._last_error = "export_failed"
            else:
                self._last_error = None
                rejected = response.partial_success.rejected_log_records
                return ("partial_success", rejected) if rejected else (None, None)
            await self._wait_retry(wait)
            delay = min(delay * 2, RETRY_MAX_SECONDS)
        return None

    async def _persist_result(
        self, identities: tuple[int, ...], reason: str | None, rejected: int | None
    ) -> None:
        if self.outbox is None:
            return
        while not self._stop.is_set():
            try:
                if reason is None:
                    await asyncio.to_thread(self.outbox.acknowledge, identities)
                else:
                    await asyncio.to_thread(
                        self.outbox.quarantine, identities, reason, rejected=rejected
                    )
            except Exception:
                self._last_error = "storage_result_failed"
                await self._wait_retry(RETRY_INITIAL_SECONDS)
            else:
                self._last_error = None
                return

    async def _deliver(self) -> None:
        if self.outbox is None:
            return
        while not self._stop.is_set():
            self._wake.clear()
            try:
                rows = await asyncio.to_thread(self.outbox.read_batch)
            except Exception:
                self._last_error = "storage_read_failed"
                await self._wait_retry(RETRY_INITIAL_SECONDS)
                continue
            if not rows:
                await self._wake.wait()
                continue
            identities, payload, invalid = _batch(rows)
            if invalid:
                await self._persist_result(invalid, "invalid_stored_payload", None)
            if not identities:
                continue
            result = await self._send(payload)
            if result is not None:
                await self._persist_result(identities, *result)

    async def shutdown(self, timeout: float = 5.0) -> None:  # ruff: ignore[async-function-with-timeout]
        if self._shutdown_task is None:
            self._closed = True
            self._shutdown_task = asyncio.create_task(self._shutdown(timeout))
        await asyncio.shield(self._shutdown_task)

    async def _shutdown(self, timeout: float) -> None:  # ruff: ignore[async-function-with-timeout, complex-structure]
        if self._writes:
            await asyncio.gather(*self._writes, return_exceptions=True)
        failures: list[BaseException] = []
        self._stop.set()
        self._wake.set()
        if self.sender is not None:
            try:
                await asyncio.to_thread(self.sender.shutdown)
            except BaseException as error:
                failures.append(error)
        if self._worker is not None:
            try:
                async with asyncio.timeout(timeout):
                    await asyncio.shield(self._worker)
            except TimeoutError:
                self._worker.cancel()
                await asyncio.gather(self._worker, return_exceptions=True)
            except BaseException as error:
                failures.append(error)
        if self.outbox is not None:
            try:
                await asyncio.to_thread(self.outbox.close)
            except BaseException as error:
                failures.append(error)
        try:
            await asyncio.to_thread(_shutdown_resources, self._resources)
        except BaseException as error:
            failures.append(error)
        if len(failures) == 1:
            raise failures[0]
        if failures:
            message = "failed to close OpenTelemetry pipeline"
            raise BaseExceptionGroup(message, failures)


def configure(  # ruff: ignore[complex-structure, too-many-statements, too-many-locals]
    *,
    outbox_path: Path,
    resource_attributes: Mapping[str, AttributeValue] | None = None,
) -> Pipeline:
    global _configured  # ruff: ignore[global-statement]
    enabled = {
        signal: _otlp_exporter_enabled(f"OTEL_{signal}_EXPORTER")
        for signal in ("METRICS", "TRACES", "LOGS")
    }
    attributes = dict(resource_attributes or {})
    instance_id = attributes.setdefault("service.instance.id", str(ULID()))
    if not isinstance(instance_id, str) or not re.fullmatch(
        r"[0-7][0-9A-HJKMNP-TV-Z]{25}", instance_id
    ):
        message = "service.instance.id must be a ULID"
        raise ValueError(message)
    resource = Resource.create(attributes)
    if str(resource.attributes.get("service.name", "")).startswith("unknown_service"):
        resource = resource.merge(Resource({"service.name": "dst-server"}))
    with _configure_lock:
        if _configured:
            message = "OTLP providers have already been configured"
            raise RuntimeError(message)
        resources: list[_ShutdownResource] = []
        pending: _ShutdownResource | None = None
        outbox: Outbox | None = None
        sender: OTLPLogSender | None = None
        try:  # ruff: ignore[too-many-statements-in-try-clause]
            readers = ()
            if enabled["METRICS"]:
                exporter = OTLPMetricExporter()
                pending = exporter
                reader = PeriodicExportingMetricReader(exporter)
                pending = reader
                readers = (reader,)
            meter_provider = MeterProvider(
                resource=resource, metric_readers=readers, shutdown_on_exit=False
            )
            resources.append(meter_provider)
            pending = None
            tracer_provider = TracerProvider(resource=resource, shutdown_on_exit=False)
            resources.insert(0, tracer_provider)
            if enabled["TRACES"]:
                span_exporter = OTLPSpanExporter(meter_provider=meter_provider)
                pending = span_exporter
                processor = BatchSpanProcessor(
                    span_exporter, meter_provider=meter_provider
                )
                pending = processor
                tracer_provider.add_span_processor(processor)
                pending = None
            if enabled["LOGS"]:
                outbox = Outbox(outbox_path)
                sender = OTLPLogSender(meter_provider=meter_provider)
            pipeline = Pipeline(resource, outbox, sender, tuple(resources))
            metrics.set_meter_provider(meter_provider)
            trace.set_tracer_provider(tracer_provider)
        except BaseException as error:
            if pending is not None:
                resources.insert(0, pending)
            if sender is not None:
                resources.insert(0, sender)
            try:
                _shutdown_resources(tuple(resources))
            except BaseException as cleanup_error:
                message = "failed to configure OpenTelemetry"
                raise BaseExceptionGroup(message, [error, cleanup_error]) from None
            finally:
                if outbox is not None:
                    outbox.close()
            raise
        _configured = True
        return pipeline
