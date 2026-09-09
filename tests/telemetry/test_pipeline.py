import asyncio
import os
import subprocess  # ruff: ignore[suspicious-subprocess-import]
import sys
from collections.abc import Sequence
from pathlib import Path
from threading import Event
from time import perf_counter, time_ns
from unittest.mock import Mock

import pytest
from opentelemetry._logs import SeverityNumber
from opentelemetry.sdk._logs import LoggerProvider, ReadableLogRecord
from opentelemetry.sdk._logs.export import (
    BatchLogRecordProcessor,
    InMemoryLogRecordExporter,
    LogRecordExportResult,
)
from opentelemetry.sdk.resources import Resource
from pydantic import JsonValue
from ulid import ULID

from dst_server.events import GAME_EVENT_ADAPTER, ObservedGameEvent
from dst_server.telemetry import otel

ATTEMPT = "01ARZ3NDEKTSV4RRFFQ69G5FAV"
INSTANCE = "01ARZ3NDEKTSV4RRFFQ69G5FAW"


@pytest.fixture(autouse=True)  # ruff: ignore[pytest-fixture-autouse]
def environment(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in tuple(os.environ):
        if name.startswith("OTEL_"):
            monkeypatch.delenv(name)
    monkeypatch.setattr(otel, "_globals_installed", True)


def observed(*, generation: int = 1, sequence: int = 1) -> ObservedGameEvent:
    event = GAME_EVENT_ADAPTER.validate_python({
        "v": 2,
        "nonce": ATTEMPT,
        "generation": generation,
        "session_id": "original-session",
        "seq": sequence,
        "event": "dst.world.state_changed",
        "tick": 10,
        "monotonic_ms": 20,
        "cycle": 2,
        "data": {"name": "cycles", "value": 2},
    })
    return ObservedGameEvent(record=event, observed_timestamp_ns=time_ns())


def make_pipeline(
    exporter: InMemoryLogRecordExporter, *, max_queue_size: int = 2048
) -> otel.Pipeline:
    resource = Resource({
        "service.name": "dst-server",
        "service.instance.id": INSTANCE,
    })
    provider = LoggerProvider(resource=resource, shutdown_on_exit=False)
    provider.add_log_record_processor(
        BatchLogRecordProcessor(
            exporter,
            schedule_delay_millis=60_000,
            max_queue_size=max_queue_size,
            max_export_batch_size=min(512, max_queue_size),
        )
    )
    return otel.Pipeline(resource=resource, logger_provider=provider)


async def test_game_logs_keep_source_identity_and_generation() -> None:
    exporter = InMemoryLogRecordExporter()
    pipeline = make_pipeline(exporter)
    event = observed()
    try:
        pipeline.emit_event(
            event,
            attributes={
                "dst.cluster.name": "dst-000",
                "dst.shard.name": "forest",
                "dst.session.id": "wrong-session",
                "dst.event.sequence": 999,
                "dst.world.cycle": 999,
            },
        )
        pipeline.emit_event(observed(generation=2))
    finally:
        await pipeline.shutdown()
    first, second = exporter.get_finished_logs()
    record = first.log_record
    assert first.resource.attributes["service.instance.id"] == INSTANCE
    assert first.instrumentation_scope is not None
    assert first.instrumentation_scope.name == "dst-server"
    assert record.event_name == "dst.world.state_changed"
    assert record.timestamp == record.observed_timestamp == event.observed_timestamp_ns
    assert record.severity_text == "INFO"
    assert record.severity_number is SeverityNumber.INFO
    assert record.body == {"name": "cycles", "value": 2}
    assert record.attributes == {
        "dst.cluster.name": "dst-000",
        "dst.shard.name": "forest",
        "log.record.uid": f"{ATTEMPT}:1:1",
        "dst.game.attempt.id": ATTEMPT,
        "dst.runtime.generation": 1,
        "dst.event.sequence": 1,
        "dst.tick": 10,
        "dst.monotonic_ms": 20,
        "dst.session.id": "original-session",
        "dst.world.cycle": 2,
    }
    assert second.log_record.attributes is not None
    assert second.log_record.attributes["log.record.uid"] == f"{ATTEMPT}:2:1"


async def test_instrumentation_failure_has_error_severity() -> None:
    exporter = InMemoryLogRecordExporter()
    pipeline = make_pipeline(exporter)
    source = observed()
    failure = GAME_EVENT_ADAPTER.validate_python(
        source.record.model_dump()
        | {
            "event": "dst.telemetry.error",
            "data": {
                "stage": "player.combat_hit",
                "message": "callback_failed",
                "count": 2,
            },
        }
    )
    try:
        pipeline.emit_event(ObservedGameEvent(failure, source.observed_timestamp_ns))
    finally:
        await pipeline.shutdown()
    record = exporter.get_finished_logs()[0].log_record
    assert record.severity_text == "ERROR"
    assert record.severity_number is SeverityNumber.ERROR


async def test_operational_logs_snapshot_body_and_preserve_or_generate_uid() -> None:
    exporter = InMemoryLogRecordExporter()
    pipeline = make_pipeline(exporter)
    timestamp = time_ns()
    details: dict[str, JsonValue] = {"list": [None, True, 42, 1.5, "中文"]}
    body: dict[str, JsonValue] = {"message": "Lua crashed", "nested": details}
    attributes = {"log.record.uid": "process-exit-1"}
    try:
        pipeline.emit_operational(
            event_name="dst.process.exited",
            body=body,
            observed_timestamp_ns=timestamp,
            severity_text="error",
            attributes=attributes,
        )
        body["message"] = "changed"
        details["list"] = []
        attributes["log.record.uid"] = "changed"
        pipeline.emit_operational(
            event_name="dst.process.exited",
            body={},
            observed_timestamp_ns=timestamp,
            severity_text="INFO",
        )
    finally:
        await pipeline.shutdown()
    first, second = (entry.log_record for entry in exporter.get_finished_logs())
    assert first.event_name == "dst.process.exited"
    assert first.timestamp == first.observed_timestamp == timestamp
    assert first.severity_text == "ERROR"
    assert first.severity_number is SeverityNumber.ERROR
    assert first.body == {
        "message": "Lua crashed",
        "nested": {"list": [None, True, 42, 1.5, "中文"]},
    }
    assert first.attributes == {"log.record.uid": "process-exit-1"}
    assert second.attributes is not None
    assert str(ULID.from_str(str(second.attributes["log.record.uid"])))


async def test_unknown_severity_fails_before_enqueue() -> None:
    exporter = InMemoryLogRecordExporter()
    pipeline = make_pipeline(exporter)
    try:
        with pytest.raises(ValueError, match="severity"):
            pipeline.emit_operational(
                event_name="dst.test",
                body={},
                observed_timestamp_ns=time_ns(),
                severity_text="invalid",
            )
    finally:
        await pipeline.shutdown()
    assert not exporter.get_finished_logs()


@pytest.mark.parametrize("raises", [False, True])
async def test_failed_batch_is_discarded_and_new_logs_continue(
    monkeypatch: pytest.MonkeyPatch, raises: bool
) -> None:
    exporter = InMemoryLogRecordExporter()
    pipeline = make_pipeline(exporter)
    assert pipeline.logger_provider is not None
    failure = Mock(
        return_value=LogRecordExportResult.FAILURE,
        side_effect=RuntimeError("offline") if raises else None,
    )
    try:
        with monkeypatch.context() as patch:
            patch.setattr(exporter, "export", failure)
            pipeline.emit_event(observed())
            await asyncio.to_thread(pipeline.logger_provider.force_flush)
            await asyncio.to_thread(pipeline.logger_provider.force_flush)
            failure.assert_called_once()
        pipeline.emit_event(observed(sequence=2))
    finally:
        await pipeline.shutdown()
    records = exporter.get_finished_logs()
    assert len(records) == 1
    assert records[0].log_record.attributes is not None
    assert records[0].log_record.attributes["dst.event.sequence"] == 2


class BlockingExporter(InMemoryLogRecordExporter):
    def __init__(self) -> None:
        super().__init__()
        self.entered = Event()
        self.release = Event()

    def export(self, batch: Sequence[ReadableLogRecord]) -> LogRecordExportResult:
        self.entered.set()
        assert self.release.wait(3), "test exporter was not released"
        return super().export(batch)


async def test_full_queue_drops_oldest_without_blocking_the_event_loop() -> None:
    exporter = BlockingExporter()
    pipeline = make_pipeline(exporter, max_queue_size=2)
    try:
        pipeline.emit_event(observed(sequence=1))
        pipeline.emit_event(observed(sequence=2))
        assert await asyncio.to_thread(exporter.entered.wait, 1)
        started = perf_counter()
        for sequence in range(3, 6):
            pipeline.emit_event(observed(sequence=sequence))
        await asyncio.sleep(0)
        assert perf_counter() - started < 1
    finally:
        exporter.release.set()
        await pipeline.shutdown()
    assert [
        entry.log_record.attributes["dst.event.sequence"]
        for entry in exporter.get_finished_logs()
        if entry.log_record.attributes is not None
    ] == [1, 2, 4, 5]


async def test_shutdown_flushes_once_and_rejects_new_events(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    exporter = InMemoryLogRecordExporter()
    shutdown = Mock(wraps=exporter.shutdown)
    monkeypatch.setattr(exporter, "shutdown", shutdown)
    pipeline = make_pipeline(exporter)
    pipeline.emit_event(observed())
    await asyncio.gather(pipeline.shutdown(), pipeline.shutdown())
    await pipeline.shutdown()
    shutdown.assert_called_once_with()
    assert len(exporter.get_finished_logs()) == 1
    with pytest.raises(RuntimeError, match="closed"):
        pipeline.emit_event(observed(sequence=2))


async def test_cancelled_shutdown_waits_for_active_export_and_closes_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    exporter = BlockingExporter()
    shutdown = Mock(wraps=exporter.shutdown)
    monkeypatch.setattr(exporter, "shutdown", shutdown)
    pipeline = make_pipeline(exporter, max_queue_size=1)
    closing = None
    outcome = None
    try:
        pipeline.emit_event(observed())
        assert await asyncio.to_thread(exporter.entered.wait, 1)
        closing = asyncio.create_task(pipeline.shutdown())
        await asyncio.sleep(0)
        with pytest.raises(RuntimeError, match="closed"):
            pipeline.emit_event(observed(sequence=2))
        closing.cancel()
        await asyncio.sleep(0)
        assert not closing.done()
        shutdown.assert_not_called()
    finally:
        exporter.release.set()
        if closing is not None:
            (outcome,) = await asyncio.gather(closing, return_exceptions=True)
        await pipeline.shutdown()
    assert isinstance(outcome, asyncio.CancelledError)
    assert len(exporter.get_finished_logs()) == 1
    shutdown.assert_called_once_with()


async def test_provider_cleanup_continues_after_one_shutdown_failure() -> None:
    logs, traces, metrics = Mock(), Mock(), Mock()
    logs.shutdown.side_effect = RuntimeError("provider failure")
    pipeline = otel.Pipeline(
        Resource({}),
        logger_provider=logs,
        tracer_provider=traces,
        meter_provider=metrics,
    )
    with pytest.raises(RuntimeError, match="provider failure"):
        await pipeline.shutdown()
    for provider in (logs, traces, metrics):
        provider.shutdown.assert_called_once_with()


async def test_configure_disabled_signals_needs_no_storage(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    for signal in ("LOGS", "METRICS", "TRACES"):
        monkeypatch.setenv(f"OTEL_{signal}_EXPORTER", "none")
    pipeline = otel.configure()
    try:
        assert not pipeline.logs_enabled
        pipeline.emit_event(observed())
    finally:
        await pipeline.shutdown()
    assert not await asyncio.to_thread(os.listdir, tmp_path)


@pytest.mark.parametrize("signal", ["LOGS", "METRICS", "TRACES"])
def test_configure_rejects_unknown_signal_exporter(
    monkeypatch: pytest.MonkeyPatch, signal: str
) -> None:
    monkeypatch.setenv(f"OTEL_{signal}_EXPORTER", "console")
    with pytest.raises(ValueError, match=f"OTEL_{signal}_EXPORTER"):
        otel.configure()


def test_configure_rejects_non_ulid_instance_id() -> None:
    with pytest.raises(ValueError, match=r"service\.instance\.id must be a ULID"):
        otel.configure(resource_attributes={"service.instance.id": "invalid"})


def test_configuration_failure_closes_created_providers_and_exporter(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for signal in ("METRICS", "TRACES"):
        monkeypatch.setenv(f"OTEL_{signal}_EXPORTER", "none")
    factories = [Mock(return_value=Mock()) for _ in range(4)]
    for name, factory in zip(
        ("MeterProvider", "TracerProvider", "LoggerProvider", "OTLPLogExporter"),
        factories,
        strict=True,
    ):
        monkeypatch.setattr(otel, name, factory)
    monkeypatch.setattr(
        otel, "BatchLogRecordProcessor", Mock(side_effect=RuntimeError("setup failure"))
    )
    with pytest.raises(RuntimeError, match="setup failure"):
        otel.configure()
    for factory in factories:
        factory.assert_called_once()
        factory.return_value.shutdown.assert_called_once_with()


def test_configure_installs_global_providers_only_once() -> None:
    source = """
import asyncio
import os
from opentelemetry import metrics, trace
from ulid import ULID
from dst_server.telemetry import otel
for signal in ("LOGS", "METRICS", "TRACES"):
    os.environ[f"OTEL_{signal}_EXPORTER"] = "none"
async def main():
    first = otel.configure()
    second = otel.configure()
    try:
        assert first.tracer_provider is not second.tracer_provider
        assert first.meter_provider is not second.meter_provider
        assert trace.get_tracer_provider() is first.tracer_provider
        assert metrics.get_meter_provider() is first.meter_provider
        identifier = first.resource.attributes["service.instance.id"]
        assert str(ULID.from_str(identifier)) == identifier
    finally:
        await second.shutdown()
        await first.shutdown()
asyncio.run(main())
"""
    subprocess.run(  # ruff: ignore[subprocess-without-shell-equals-true]
        [sys.executable, "-c", source], check=True, timeout=10
    )
