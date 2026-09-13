import asyncio
import os
from collections.abc import AsyncIterator
from pathlib import Path
from time import time_ns

import grpc
import pytest
from opentelemetry.proto.collector.logs.v1.logs_service_pb2 import (
    ExportLogsServiceRequest,
    ExportLogsServiceResponse,
)
from opentelemetry.proto.collector.logs.v1.logs_service_pb2_grpc import (
    LogsServiceServicer,
    add_LogsServiceServicer_to_server,
)
from opentelemetry.proto.collector.metrics.v1.metrics_service_pb2 import (
    ExportMetricsServiceRequest,
    ExportMetricsServiceResponse,
)
from opentelemetry.proto.collector.metrics.v1.metrics_service_pb2_grpc import (
    MetricsServiceServicer,
    add_MetricsServiceServicer_to_server,
)

from dst_server.telemetry import otel


class Receiver(LogsServiceServicer, MetricsServiceServicer):
    def __init__(self) -> None:
        self.available = False
        self.received: asyncio.Queue[
            tuple[ExportLogsServiceRequest, dict[str, str | bytes]]
        ] = asyncio.Queue()
        self.metrics: asyncio.Queue[ExportMetricsServiceRequest] = asyncio.Queue()

    async def Export(  # ruff: ignore[invalid-function-name]
        self,
        request: ExportLogsServiceRequest | ExportMetricsServiceRequest,
        context: grpc.aio.ServicerContext,
    ) -> ExportLogsServiceResponse | ExportMetricsServiceResponse:
        if isinstance(request, ExportMetricsServiceRequest):
            self.metrics.put_nowait(request)
            return ExportMetricsServiceResponse()
        self.received.put_nowait((request, dict(context.invocation_metadata() or ())))
        if not self.available:
            await context.abort(grpc.StatusCode.UNAVAILABLE, "collector unavailable")
        return ExportLogsServiceResponse()


@pytest.fixture
async def receiver() -> AsyncIterator[tuple[Receiver, str]]:
    service = Receiver()
    server = grpc.aio.server()
    add_LogsServiceServicer_to_server(service, server)
    add_MetricsServiceServicer_to_server(service, server)
    port = server.add_insecure_port("127.0.0.1:0")
    await server.start()
    try:
        yield service, f"http://127.0.0.1:{port}"
    finally:
        await server.stop(None)


async def test_configured_exporter_resumes_and_exports_loss_metrics(
    receiver: tuple[Receiver, str],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    service, endpoint = receiver
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("OTEL_TRACES_EXPORTER", "none")
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://127.0.0.1:1")
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_LOGS_ENDPOINT", endpoint)
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_METRICS_ENDPOINT", endpoint)
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_HEADERS", "x-generic=ignored")
    monkeypatch.setenv(
        "OTEL_EXPORTER_OTLP_LOGS_HEADERS", "x-test=value%20with%20spaces"
    )
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_LOGS_COMPRESSION", "gzip")
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_LOGS_TIMEOUT", "0.3")
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_METRICS_TIMEOUT", "0.3")
    monkeypatch.setenv("OTEL_BLRP_SCHEDULE_DELAY", "60000")
    monkeypatch.setenv("OTEL_BLRP_MAX_QUEUE_SIZE", "8")
    monkeypatch.setenv("OTEL_BLRP_MAX_EXPORT_BATCH_SIZE", "8")
    pipeline = otel.configure()
    assert pipeline.logger_provider is not None
    assert pipeline.meter_provider is not None
    try:
        for uid in ("lost", "received"):
            pipeline.emit_operational(
                event_name="dst.test.export",
                body={"message": uid},
                observed_timestamp_ns=time_ns(),
                severity_text="INFO",
                attributes={"log.record.uid": uid},
            )
            async with asyncio.timeout(2):
                await asyncio.to_thread(pipeline.logger_provider.force_flush)
            request, metadata = service.received.get_nowait()
            record = request.resource_logs[0].scope_logs[0].log_records[0]
            assert record.event_name == "dst.test.export"
            assert record.body.kvlist_value.values[0].value.string_value == uid
            assert metadata["x-test"] == "value with spaces"
            assert "x-generic" not in metadata
            assert service.received.empty()
            async with asyncio.timeout(2):
                await asyncio.to_thread(pipeline.meter_provider.force_flush)
            metrics_request = service.metrics.get_nowait()
            metrics = {
                metric.name: metric.sum.data_points
                for resource in metrics_request.resource_metrics
                for scope in resource.scope_metrics
                for metric in scope.metrics
            }
            assert [
                point.as_int for point in metrics["otel.sdk.processor.log.queue.size"]
            ] == [0]
            assert [
                point.as_int
                for point in metrics["otel.sdk.processor.log.queue.capacity"]
            ] == [8]
            exported = {
                any(attr.key == "error.type" for attr in point.attributes): (
                    point.as_int
                )
                for point in metrics["otel.sdk.exporter.log.exported"]
            }
            assert exported[True] == 1
            assert exported.get(False, 0) == int(service.available)
            assert service.metrics.empty()
            service.available = True
    finally:
        async with asyncio.timeout(2):
            await pipeline.shutdown()
            await pipeline.shutdown()
    assert service.received.empty()
    assert not await asyncio.to_thread(os.listdir, tmp_path)
