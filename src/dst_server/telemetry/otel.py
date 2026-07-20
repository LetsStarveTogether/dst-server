from __future__ import annotations

import asyncio
from collections.abc import Mapping
from dataclasses import dataclass, field
from types import TracebackType
from typing import TYPE_CHECKING, Self
from uuid import uuid4

from opentelemetry import metrics, trace
from opentelemetry._logs import (  # ruff:ignore[import-private-name]
    Logger,
    SeverityNumber,
    set_logger_provider,
)
from opentelemetry.exporter.otlp.proto.grpc._log_exporter import (  # ruff:ignore[import-private-name]
    OTLPLogExporter,
)
from opentelemetry.exporter.otlp.proto.grpc.metric_exporter import (
    OTLPMetricExporter,
)
from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import (
    OTLPSpanExporter,
)
from opentelemetry.sdk._logs import (  # ruff:ignore[import-private-name]
    LoggerProvider,
)
from opentelemetry.sdk._logs.export import (  # ruff:ignore[import-private-name]
    BatchLogRecordProcessor,
)
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from opentelemetry.util.types import AttributeValue

if TYPE_CHECKING:
    from dst_server.events import ObservedGameEvent

configured = False


@dataclass(slots=True)
class Pipeline:
    logger: Logger
    tracer_provider: TracerProvider
    meter_provider: MeterProvider
    logger_provider: LoggerProvider
    closed: bool = field(default=False, init=False)

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        exc_tb: TracebackType | None,
    ) -> None:
        _ = exc_type, exc, exc_tb
        await self.shutdown()

    async def force_flush(self, timeout_millis: int = 30_000) -> bool:
        if (
            isinstance(timeout_millis, bool)
            or not isinstance(timeout_millis, int)
            or timeout_millis <= 0
        ):
            msg = "timeout_millis must be a positive integer"
            raise ValueError(msg)
        if self.closed:
            return False
        return await asyncio.to_thread(self.force_flush_sync, timeout_millis)

    async def shutdown(self) -> None:
        if self.closed:
            return
        self.closed = True
        await asyncio.to_thread(self.shutdown_sync)

    def force_flush_sync(self, timeout_millis: int) -> bool:
        results = (
            self.logger_provider.force_flush(timeout_millis),
            self.meter_provider.force_flush(timeout_millis),
            self.tracer_provider.force_flush(timeout_millis),
        )
        return all(results)

    def shutdown_sync(self) -> None:
        try:
            self.logger_provider.shutdown()
        finally:
            try:
                self.meter_provider.shutdown()
            finally:
                self.tracer_provider.shutdown()


def configure(
    *,
    service_instance_id: str | None = None,
    resource_attributes: Mapping[str, AttributeValue] | None = None,
) -> Pipeline:
    global configured  # ruff:ignore[global-statement]
    if configured:
        msg = "OTLP providers have already been configured"
        raise RuntimeError(msg)
    if service_instance_id is not None and (
        not isinstance(service_instance_id, str) or not service_instance_id
    ):
        msg = "service_instance_id must be a non-empty string"
        raise ValueError(msg)

    attributes = dict(resource_attributes or {})
    if service_instance_id is not None:
        attributes["service.instance.id"] = service_instance_id
    resource = Resource.create(attributes)
    defaults: dict[str, AttributeValue] = {}
    if str(resource.attributes.get("service.name", "")).startswith("unknown_service"):
        defaults["service.name"] = "dst-server"
    if "service.instance.id" not in resource.attributes:
        defaults["service.instance.id"] = str(uuid4())
    resource = resource.merge(Resource(defaults))

    metric_reader = PeriodicExportingMetricReader(OTLPMetricExporter())
    meter_provider = MeterProvider(
        resource=resource,
        metric_readers=(metric_reader,),
        shutdown_on_exit=False,
    )
    tracer_provider = TracerProvider(
        resource=resource,
        shutdown_on_exit=False,
    )
    tracer_provider.add_span_processor(
        BatchSpanProcessor(
            OTLPSpanExporter(meter_provider=meter_provider),
            meter_provider=meter_provider,
        )
    )
    logger_provider = LoggerProvider(
        resource=resource,
        shutdown_on_exit=False,
    )
    logger_provider.add_log_record_processor(
        BatchLogRecordProcessor(
            OTLPLogExporter(meter_provider=meter_provider),
            meter_provider=meter_provider,
        )
    )

    metrics.set_meter_provider(meter_provider)
    trace.set_tracer_provider(tracer_provider)
    set_logger_provider(logger_provider)
    configured = True
    return Pipeline(
        logger=logger_provider.get_logger("dst-server.game-events"),
        tracer_provider=tracer_provider,
        meter_provider=meter_provider,
        logger_provider=logger_provider,
    )


def emit(
    logger: Logger,
    observed: ObservedGameEvent,
    *,
    attributes: Mapping[str, AttributeValue] | None = None,
) -> None:
    event = observed.record
    values: dict[str, AttributeValue] = dict(attributes or {}) | {
        "dst.event.sequence": event.seq,
        "dst.tick": event.tick,
        "dst.monotonic_ms": event.monotonic_ms,
    }
    if event.cycle is not None:
        values["dst.world.cycle"] = event.cycle
    logger.emit(
        timestamp=observed.observed_timestamp_ns,
        observed_timestamp=observed.observed_timestamp_ns,
        severity_number=SeverityNumber.INFO,
        severity_text="INFO",
        event_name=event.event,
        body=event.data.model_dump(mode="json"),
        attributes=values,
    )


__all__ = [
    "Pipeline",
    "configure",
    "emit",
]
