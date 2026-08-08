from __future__ import annotations

import asyncio
import os
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from threading import Lock
from typing import TYPE_CHECKING, Protocol, Self

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
from ulid import ULID

if TYPE_CHECKING:
    from dst_server.events import ObservedGameEvent

_configured = False
_configure_lock = Lock()


class _ShutdownResource(Protocol):
    def shutdown(self) -> object: ...


def _otlp_exporter_enabled(variable: str) -> bool:
    value = os.environ.get(variable, "").casefold()
    if not value or value == "otlp":
        return True
    if value == "none":
        return False
    msg = f"{variable} must be 'otlp' or 'none'"
    raise ValueError(msg)


def _shutdown_resources(
    resources: tuple[_ShutdownResource, ...],
) -> None:
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


@dataclass(slots=True)
class Pipeline:
    logger: Logger | None
    tracer_provider: TracerProvider
    meter_provider: MeterProvider
    logger_provider: LoggerProvider
    closed: bool = field(default=False, init=False)

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *_: object) -> None:
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
        results: list[bool] = []
        failures: list[BaseException] = []
        for provider in (
            self.logger_provider,
            self.tracer_provider,
            self.meter_provider,
        ):
            try:
                results.append(provider.force_flush(timeout_millis))
            except BaseException as error:
                failures.append(error)
        if len(failures) == 1:
            raise failures[0]
        if failures:
            message = "failed to flush OpenTelemetry providers"
            raise BaseExceptionGroup(message, failures)
        return all(results)

    def shutdown_sync(self) -> None:
        _shutdown_resources((
            self.logger_provider,
            self.tracer_provider,
            self.meter_provider,
        ))


def configure(  # ruff: ignore[complex-structure, too-many-branches, too-many-locals, too-many-statements]
    *,
    resource_attributes: Mapping[str, AttributeValue] | None = None,
) -> Pipeline:
    global _configured  # ruff:ignore[global-statement]
    with _configure_lock:
        if _configured:
            msg = "OTLP providers have already been configured"
            raise RuntimeError(msg)
        attributes = dict(resource_attributes or {})
        instance_id = attributes.get("service.instance.id")
        if instance_id is None:
            instance_id = str(ULID())
        elif not isinstance(instance_id, str) or not re.fullmatch(
            r"[0-7][0-9A-HJKMNP-TV-Z]{25}", instance_id
        ):
            msg = "service.instance.id must be a ULID"
            raise ValueError(msg)
        attributes["service.instance.id"] = instance_id
        resource = Resource.create(attributes)
        if str(resource.attributes.get("service.name", "")).startswith(
            "unknown_service"
        ):
            resource = resource.merge(Resource({"service.name": "dst-server"}))

        metric_exporter: OTLPMetricExporter | None = None
        metric_reader: PeriodicExportingMetricReader | None = None
        meter_provider: MeterProvider | None = None
        tracer_provider: TracerProvider | None = None
        logger_provider: LoggerProvider | None = None
        span_exporter: OTLPSpanExporter | None = None
        span_processor: BatchSpanProcessor | None = None
        log_exporter: OTLPLogExporter | None = None
        log_processor: BatchLogRecordProcessor | None = None
        event_logger: Logger | None = None
        try:  # ruff:ignore[too-many-statements-in-try-clause]
            if _otlp_exporter_enabled("OTEL_METRICS_EXPORTER"):
                metric_exporter = OTLPMetricExporter()
                metric_reader = PeriodicExportingMetricReader(metric_exporter)
            meter_provider = MeterProvider(
                resource=resource,
                metric_readers=(metric_reader,) if metric_reader is not None else (),
                shutdown_on_exit=False,
            )
            tracer_provider = TracerProvider(
                resource=resource,
                shutdown_on_exit=False,
            )
            if _otlp_exporter_enabled("OTEL_TRACES_EXPORTER"):
                span_exporter = OTLPSpanExporter(meter_provider=meter_provider)
                span_processor = BatchSpanProcessor(
                    span_exporter,
                    meter_provider=meter_provider,
                )
                tracer_provider.add_span_processor(span_processor)
                span_exporter = span_processor = None
            logger_provider = LoggerProvider(
                resource=resource,
                shutdown_on_exit=False,
            )
            if _otlp_exporter_enabled("OTEL_LOGS_EXPORTER"):
                log_exporter = OTLPLogExporter(meter_provider=meter_provider)
                log_processor = BatchLogRecordProcessor(
                    log_exporter,
                    meter_provider=meter_provider,
                )
                logger_provider.add_log_record_processor(log_processor)
                log_exporter = log_processor = None
                event_logger = logger_provider.get_logger("dst-server.game-events")

            pipeline = Pipeline(
                logger=event_logger,
                tracer_provider=tracer_provider,
                meter_provider=meter_provider,
                logger_provider=logger_provider,
            )
            metrics.set_meter_provider(pipeline.meter_provider)
            trace.set_tracer_provider(pipeline.tracer_provider)
            set_logger_provider(pipeline.logger_provider)
        except BaseException as error:
            try:  # ruff:ignore[too-many-statements-in-try-clause]
                pending: list[_ShutdownResource] = []
                if log_processor is not None:
                    pending.append(log_processor)
                elif log_exporter is not None:
                    pending.append(log_exporter)
                if span_processor is not None:
                    pending.append(span_processor)
                elif span_exporter is not None:
                    pending.append(span_exporter)
                if meter_provider is None:
                    if metric_reader is not None:
                        pending.append(metric_reader)
                    elif metric_exporter is not None:
                        pending.append(metric_exporter)
                else:
                    pending.extend(
                        provider
                        for provider in (
                            logger_provider,
                            tracer_provider,
                            meter_provider,
                        )
                        if provider is not None
                    )
                _shutdown_resources(tuple(pending))
            except BaseException as cleanup_error:
                message = "failed to configure OpenTelemetry"
                raise BaseExceptionGroup(message, [error, cleanup_error]) from None
            raise
        _configured = True
        return pipeline


def emit(
    logger: Logger,
    observed: ObservedGameEvent,
    *,
    attributes: Mapping[str, AttributeValue] | None = None,
) -> None:
    event = observed.record
    values: dict[str, AttributeValue] = dict(attributes or {})
    values["dst.event.sequence"] = event.seq
    values["dst.tick"] = event.tick
    values["dst.monotonic_ms"] = event.monotonic_ms
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
