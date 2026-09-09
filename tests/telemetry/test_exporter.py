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

from dst_server.telemetry import otel


@pytest.fixture(autouse=True)  # ruff: ignore[pytest-fixture-autouse]
def environment(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in tuple(os.environ):
        if name.startswith("OTEL_"):
            monkeypatch.delenv(name)
    monkeypatch.setattr(otel, "_globals_installed", True)
    for signal in ("METRICS", "TRACES"):
        monkeypatch.setenv(f"OTEL_{signal}_EXPORTER", "none")


class Receiver(LogsServiceServicer):
    def __init__(self) -> None:
        self.available = False
        self.received: asyncio.Queue[
            tuple[ExportLogsServiceRequest, dict[str, str | bytes]]
        ] = asyncio.Queue()

    async def Export(  # ruff: ignore[invalid-function-name]
        self, request: ExportLogsServiceRequest, context: grpc.aio.ServicerContext
    ) -> ExportLogsServiceResponse:
        self.received.put_nowait((request, dict(context.invocation_metadata() or ())))
        if not self.available:
            await context.abort(grpc.StatusCode.UNAVAILABLE, "collector unavailable")
        return ExportLogsServiceResponse()


@pytest.fixture
async def receiver() -> AsyncIterator[tuple[Receiver, str]]:
    service = Receiver()
    server = grpc.aio.server()
    add_LogsServiceServicer_to_server(service, server)
    port = server.add_insecure_port("127.0.0.1:0")
    await server.start()
    try:
        yield service, f"http://127.0.0.1:{port}"
    finally:
        await server.stop(None)


async def test_configured_exporter_drops_failed_logs_and_resumes_with_metadata(
    receiver: tuple[Receiver, str],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    service, endpoint = receiver
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://127.0.0.1:1")
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_LOGS_ENDPOINT", endpoint)
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_HEADERS", "x-generic=ignored")
    monkeypatch.setenv(
        "OTEL_EXPORTER_OTLP_LOGS_HEADERS", "x-test=value%20with%20spaces"
    )
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_LOGS_COMPRESSION", "gzip")
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_LOGS_TIMEOUT", "0.3")
    monkeypatch.setenv("OTEL_BLRP_SCHEDULE_DELAY", "60000")
    pipeline = otel.configure()
    assert pipeline.logger_provider is not None
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
            service.available = True
    finally:
        async with asyncio.timeout(2):
            await pipeline.shutdown()
            await pipeline.shutdown()
    assert service.received.empty()
    assert not await asyncio.to_thread(os.listdir, tmp_path)
