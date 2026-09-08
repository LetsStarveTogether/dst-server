import asyncio
import os
import random
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from threading import Lock
from typing import TYPE_CHECKING, Protocol, cast

import grpc
from opentelemetry import metrics, trace
from opentelemetry.exporter.otlp.proto.grpc.metric_exporter import OTLPMetricExporter
from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from opentelemetry.util.types import AttributeValue
from pydantic import JsonValue
from ulid import ULID

from dst_server.concurrency import cancel_tasks, complete
from dst_server.models.telemetry import DeliveryStatus

from .exporter import (
    RETRYABLE_CODES,
    LogsExporter,
    OTLPSettings,
    batch,
    encode_log,
    retry_after,
)
from .outbox import Outbox

if TYPE_CHECKING:
    from dst_server.events import ObservedGameEvent

RETRY_INITIAL_SECONDS = 1.0
RETRY_MAX_SECONDS = 30.0
_globals_installed = False
_global_lock = Lock()


class _ShutdownResource(Protocol):
    def shutdown(self) -> object: ...


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


def _otlp_exporter_enabled(variable: str) -> bool:
    value = os.environ.get(variable, "").casefold()
    if not value or value == "otlp":
        return True
    if value == "none":
        return False
    message = f"{variable} must be 'otlp' or 'none'"
    raise ValueError(message)


@dataclass(slots=True)
class Pipeline:
    resource: Resource
    outbox: Outbox | None = None
    sender: LogsExporter | None = None
    meter_provider: MeterProvider | None = field(default=None, repr=False)
    tracer_provider: TracerProvider | None = field(default=None, repr=False)
    _closed: bool = field(default=False, init=False)
    _worker: asyncio.Task[None] | None = field(default=None, init=False)
    _shutdown_task: asyncio.Task[None] | None = field(default=None, init=False)
    _writes: set[asyncio.Task[int]] = field(default_factory=set, init=False)
    _wake: asyncio.Event = field(default_factory=asyncio.Event, init=False)
    _last_error: str | None = field(default=None, init=False)

    def __post_init__(self) -> None:
        if (self.outbox is None) != (self.sender is None):
            message = "OTLP Logs requires both an outbox and an exporter"
            raise ValueError(message)
        if self.outbox is not None and self.sender is not None:
            self._worker = asyncio.create_task(
                self._deliver(self.outbox, self.sender), name="dst-otel-delivery"
            )

    @property
    def logs_enabled(self) -> bool:
        return self.outbox is not None

    def status(self) -> DeliveryStatus:
        status = self.outbox.stats() if self.outbox is not None else DeliveryStatus()
        return (
            status.replace(last_error=self._last_error) if self._last_error else status
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
        payload = encode_log(
            self.resource,
            event_name=event_name,
            body=body,
            observed_timestamp_ns=observed_timestamp_ns,
            severity_text=severity_text,
            attributes=attributes,
        )
        pending = asyncio.create_task(asyncio.to_thread(self.outbox.append, payload))
        self._writes.add(pending)
        try:
            await complete(pending)
        finally:
            if not pending.cancelled() and pending.exception() is not None:
                self._last_error = "storage_write_failed"
            self._writes.discard(pending)
            self._wake.set()

    async def _wait_retry(self, delay: float) -> None:
        await asyncio.sleep(delay)

    async def _send(
        self, sender: LogsExporter, payload: bytes
    ) -> tuple[str | None, int | None]:
        delay = RETRY_INITIAL_SECONDS
        while True:
            wait = delay * random.uniform(0.8, 1.2)  # ruff: ignore[suspicious-non-cryptographic-random-usage]
            try:
                response = await sender.export(payload)
            except grpc.RpcError as error:
                code = cast("grpc.Call", error).code()
                retry = retry_after(error)
                reason = f"export_{code.name.lower()}" if code else "export_unknown"
                self._last_error = reason
                if code not in RETRYABLE_CODES and not (
                    code is grpc.StatusCode.RESOURCE_EXHAUSTED and retry is not None
                ):
                    return reason, None
                if retry is not None:
                    wait = retry
            except Exception:
                self._last_error = "export_failed"
            else:
                self._last_error = None
                rejected = response.partial_success.rejected_log_records
                if rejected < 0:
                    return "invalid_export_response", None
                return ("partial_success", rejected) if rejected else (None, None)
            await self._wait_retry(wait)
            delay = min(delay * 2, RETRY_MAX_SECONDS)

    async def _persist_result(
        self,
        outbox: Outbox,
        identities: tuple[int, ...],
        reason: str | None,
        rejected: int | None,
    ) -> None:
        while True:
            try:
                if reason is None:
                    await complete(asyncio.to_thread(outbox.acknowledge, identities))
                else:
                    await complete(
                        asyncio.to_thread(
                            outbox.quarantine, identities, reason, rejected=rejected
                        )
                    )
            except Exception:
                self._last_error = "storage_result_failed"
                await self._wait_retry(RETRY_INITIAL_SECONDS)
            else:
                self._last_error = None
                return

    async def _deliver(self, outbox: Outbox, sender: LogsExporter) -> None:
        while True:
            self._wake.clear()
            try:
                rows = await complete(asyncio.to_thread(outbox.read_batch))
            except Exception:
                self._last_error = "storage_read_failed"
                await self._wait_retry(RETRY_INITIAL_SECONDS)
                continue
            if not rows:
                await self._wake.wait()
                continue
            identities, payload, invalid = batch(rows)
            if invalid:
                await self._persist_result(
                    outbox, invalid, "invalid_stored_payload", None
                )
            if identities:
                await self._persist_result(
                    outbox, identities, *await self._send(sender, payload)
                )

    async def shutdown(self) -> None:
        if self._shutdown_task is None:
            self._closed = True
            self._shutdown_task = asyncio.create_task(self._shutdown())
        await complete(self._shutdown_task)

    async def _shutdown(self) -> None:
        if self._writes:
            await asyncio.gather(*self._writes, return_exceptions=True)
        if self._worker is not None:
            await cancel_tasks(self._worker)
        failures: list[BaseException] = []
        if self.sender is not None:
            try:
                await self.sender.aclose()
            except BaseException as error:
                failures.append(error)
        if self.outbox is not None:
            try:
                await asyncio.to_thread(self.outbox.close)
            except BaseException as error:
                failures.append(error)
        resources = tuple(
            provider
            for provider in (self.tracer_provider, self.meter_provider)
            if provider is not None
        )
        try:
            await asyncio.to_thread(_shutdown_resources, resources)
        except BaseException as error:
            failures.append(error)
        if len(failures) == 1:
            raise failures[0]
        if failures:
            message = "failed to close OpenTelemetry pipeline"
            raise BaseExceptionGroup(message, failures)


def _providers(
    resource: Resource, enabled: Mapping[str, bool]
) -> tuple[MeterProvider, TracerProvider]:
    resources: list[_ShutdownResource] = []
    pending: _ShutdownResource | None = None
    try:  # ruff: ignore[too-many-statements-in-try-clause]
        readers = ()
        if enabled["METRICS"]:
            metric_exporter = OTLPMetricExporter()
            pending = metric_exporter
            reader = PeriodicExportingMetricReader(metric_exporter)
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
            processor = BatchSpanProcessor(span_exporter, meter_provider=meter_provider)
            pending = processor
            tracer_provider.add_span_processor(processor)
            pending = None
    except BaseException as error:
        if pending is not None:
            resources.insert(0, pending)
        try:
            _shutdown_resources(tuple(resources))
        except BaseException as cleanup_error:
            message = "failed to configure OpenTelemetry providers"
            raise BaseExceptionGroup(message, [error, cleanup_error]) from None
        raise
    return meter_provider, tracer_provider


def configure(
    *,
    outbox_path: Path,
    resource_attributes: Mapping[str, AttributeValue] | None = None,
) -> Pipeline:
    global _globals_installed  # ruff: ignore[global-statement]
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
    settings = OTLPSettings.from_environment() if enabled["LOGS"] else None
    if settings is not None:
        asyncio.get_running_loop()
    meter_provider, tracer_provider = _providers(resource, enabled)
    outbox = None
    try:
        sender = None
        if settings is not None:
            outbox = Outbox(outbox_path)
            sender = LogsExporter(settings)
        pipeline = Pipeline(resource, outbox, sender, meter_provider, tracer_provider)
    except BaseException as error:
        try:
            _shutdown_resources((tracer_provider, meter_provider))
        except BaseException as cleanup_error:
            message = "failed to configure OpenTelemetry"
            raise BaseExceptionGroup(message, [error, cleanup_error]) from None
        finally:
            if outbox is not None:
                outbox.close()
        raise
    with _global_lock:
        if not _globals_installed:
            metrics.set_meter_provider(meter_provider)
            trace.set_tracer_provider(tracer_provider)
            _globals_installed = True
    return pipeline
