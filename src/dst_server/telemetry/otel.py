import asyncio
import os
import re
from collections.abc import Mapping
from copy import deepcopy
from dataclasses import dataclass, field
from threading import Lock
from typing import TYPE_CHECKING, Protocol, cast

from opentelemetry import metrics, trace
from opentelemetry._logs import Logger, SeverityNumber  # ruff: ignore[import-private-name]
from opentelemetry.exporter.otlp.proto.grpc._log_exporter import (  # ruff: ignore[import-private-name]
    OTLPLogExporter,
)
from opentelemetry.exporter.otlp.proto.grpc.metric_exporter import OTLPMetricExporter
from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter
from opentelemetry.sdk._logs import LoggerProvider  # ruff: ignore[import-private-name]
from opentelemetry.sdk._logs.export import (  # ruff: ignore[import-private-name]
    BatchLogRecordProcessor,
)
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from opentelemetry.util.types import AttributeValue
from pydantic import JsonValue
from ulid import ULID

from dst_server.concurrency import complete

if TYPE_CHECKING:
    from dst_server.events import ObservedGameEvent

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
    logger_provider: LoggerProvider | None = field(default=None, repr=False)
    meter_provider: MeterProvider | None = field(default=None, repr=False)
    tracer_provider: TracerProvider | None = field(default=None, repr=False)
    _logger: Logger | None = field(default=None, init=False, repr=False)
    _closed: bool = field(default=False, init=False)
    _shutdown_task: asyncio.Task[None] | None = field(default=None, init=False)

    def __post_init__(self) -> None:
        if self.logger_provider is not None:
            self._logger = self.logger_provider.get_logger("dst-server")

    @property
    def logs_enabled(self) -> bool:
        return self._logger is not None

    def emit_event(
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
        self.emit_operational(
            event_name=event.event,
            body=event.data.model_dump(mode="json"),
            observed_timestamp_ns=observed.observed_timestamp_ns,
            severity_text="ERROR" if event.event == "dst.telemetry.error" else "INFO",
            attributes=values,
        )

    def emit_operational(
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
        if self._logger is None:
            return
        values = dict(attributes or {})
        values.setdefault("log.record.uid", str(ULID()))
        severity_text = severity_text.upper()
        try:
            severity_number = SeverityNumber[severity_text]
        except KeyError:
            message = "unknown OpenTelemetry severity"
            raise ValueError(message) from None
        self._logger.emit(
            event_name=event_name,
            body=deepcopy(dict(body)),
            timestamp=observed_timestamp_ns,
            observed_timestamp=observed_timestamp_ns,
            severity_text=severity_text,
            severity_number=severity_number,
            attributes=values,
        )

    async def shutdown(self) -> None:
        if self._shutdown_task is None:
            self._closed = True
            resources = tuple(
                provider
                for provider in (
                    self.logger_provider,
                    self.tracer_provider,
                    self.meter_provider,
                )
                if provider is not None
            )
            self._shutdown_task = asyncio.create_task(
                asyncio.to_thread(_shutdown_resources, resources)
            )
        await complete(self._shutdown_task)


def _create_pipeline(resource: Resource, enabled: Mapping[str, bool]) -> Pipeline:
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
        logger_provider = None
        if enabled["LOGS"]:
            logger_provider = LoggerProvider(
                resource=resource, shutdown_on_exit=False, meter_provider=meter_provider
            )
            resources.insert(0, logger_provider)
            log_exporter = OTLPLogExporter(meter_provider=meter_provider)
            pending = log_exporter
            log_processor = BatchLogRecordProcessor(
                log_exporter, meter_provider=meter_provider
            )
            pending = log_processor
            logger_provider.add_log_record_processor(log_processor)
            pending = None
        return Pipeline(resource, logger_provider, meter_provider, tracer_provider)
    except BaseException as error:
        if pending is not None:
            resources.insert(0, pending)
        try:
            _shutdown_resources(tuple(resources))
        except BaseException as cleanup_error:
            message = "failed to configure OpenTelemetry providers"
            raise BaseExceptionGroup(message, [error, cleanup_error]) from None
        raise


def configure(
    *,
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
    pipeline = _create_pipeline(resource, enabled)
    with _global_lock:
        if not _globals_installed:
            metrics.set_meter_provider(cast("MeterProvider", pipeline.meter_provider))
            trace.set_tracer_provider(cast("TracerProvider", pipeline.tracer_provider))
            _globals_installed = True
    return pipeline
