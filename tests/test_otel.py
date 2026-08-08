from __future__ import annotations

import subprocess  # ruff:ignore[suspicious-subprocess-import]
import sys
from pathlib import Path
from time import time_ns
from unittest.mock import AsyncMock, Mock

import pytest

pytest.importorskip("opentelemetry.sdk._logs")

from opentelemetry.sdk._logs import LoggerProvider
from opentelemetry.sdk._logs.export import (
    InMemoryLogRecordExporter,
    SimpleLogRecordProcessor,
)
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import InMemoryMetricReader
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import (
    InMemorySpanExporter,
)
from opentelemetry.trace import StatusCode
from ulid import ULID

from dst_server.events import GAME_EVENT_ADAPTER, GameEvent, ObservedGameEvent
from dst_server.game import GameClient
from dst_server.telemetry import TelemetrySettings, otel, stream
from dst_server.telemetry.otel import emit
from dst_server.telemetry.recorder import Recorder
from tests.helpers import structured_result


def player_action(nonce: str) -> GameEvent:
    return GAME_EVENT_ADAPTER.validate_python(
        {
            "v": 1,
            "nonce": nonce,
            "seq": 1,
            "event": "dst.player.action",
            "tick": 10,
            "monotonic_ms": 20,
            "cycle": 2,
            "data": {
                "action_id": "CHOP",
                "action_sequence": 1,
                "success": True,
                "reason": None,
                "actor": {
                    "prefab": "wilson",
                    "guid": 42,
                    "userid": "KU_TEST",
                    "position": {"x": 1.0, "y": 0.0, "z": 2.0},
                },
                "target": None,
                "initial_target_owner": None,
                "inventory_object": None,
                "position": None,
                "recipe": None,
                "forced": False,
            },
        },
        strict=True,
    )


async def exercise_pipeline(
    game: GameClient,
    events: stream.EventStream,
    pipeline: otel.Pipeline,
) -> int:
    tracer = pipeline.tracer_provider.get_tracer("tests")
    with tracer.start_as_current_span("bot.operation") as parent:
        parent_span_id = parent.get_span_context().span_id
        await game.world.pause(True)
        game.recorder.set_player_count(1)
    with pytest.raises(RuntimeError, match="boom"):
        await game.world.request_save()
    event = player_action(events.nonce)
    assert events.accept(
        "DST_OTEL|" + event.model_dump_json(),
        time_ns(),
    )
    observed = await events.read()
    assert observed is not None
    logger = pipeline.logger
    assert logger is not None
    emit(
        logger,
        observed,
        attributes={
            "dst.cluster.name": "cluster",
            "dst.shard.name": "test",
            "dst.session.id": "TEST",
        },
    )
    assert await pipeline.force_flush()
    return parent_span_id


def run_python(source: str) -> None:
    subprocess.run(  # ruff:ignore[subprocess-without-shell-equals-true]
        [sys.executable, "-c", source], check=True, timeout=10
    )


def test_emit_as_structured_otel_event() -> None:
    exporter = InMemoryLogRecordExporter()
    provider = LoggerProvider(shutdown_on_exit=False)
    provider.add_log_record_processor(SimpleLogRecordProcessor(exporter))
    logger = provider.get_logger("dst-server.game-events", "0.1.0")
    observed_timestamp_ns = time_ns()
    event = GAME_EVENT_ADAPTER.validate_python(
        {
            "v": 1,
            "nonce": "01ARZ3NDEKTSV4RRFFQ69G5FAV",
            "seq": 1,
            "event": "dst.world.state_changed",
            "tick": 10,
            "monotonic_ms": 20,
            "cycle": 2,
            "data": {"name": "cycles", "value": 2},
        },
        strict=True,
    )

    try:
        emit(
            logger,
            ObservedGameEvent(
                record=event,
                observed_timestamp_ns=observed_timestamp_ns,
            ),
            attributes={"dst.cluster.name": "test", "dst.tick": 999},
        )

        (readable,) = exporter.get_finished_logs()
        record = readable.log_record
        assert record.event_name == "dst.world.state_changed"
        assert record.body == {"name": "cycles", "value": 2}
        assert record.timestamp == observed_timestamp_ns
        assert record.observed_timestamp == observed_timestamp_ns
        assert record.attributes == {
            "dst.event.sequence": 1,
            "dst.tick": 10,
            "dst.monotonic_ms": 20,
            "dst.world.cycle": 2,
            "dst.cluster.name": "test",
        }
    finally:
        provider.shutdown()


def test_configure_rejects_non_ulid_instance_id() -> None:
    with pytest.raises(ValueError, match=r"service\.instance\.id must be a ULID"):
        otel.configure(resource_attributes={"service.instance.id": "dst-test-instance"})


def test_configure_rejects_unknown_signal_exporter(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OTEL_METRICS_EXPORTER", "console")

    with pytest.raises(
        ValueError,
        match="OTEL_METRICS_EXPORTER must be 'otlp' or 'none'",
    ):
        otel.configure()


@pytest.mark.parametrize(
    ("signal", "exporter_name", "owner_name"),
    [
        ("METRICS", "OTLPMetricExporter", "PeriodicExportingMetricReader"),
        ("TRACES", "OTLPSpanExporter", "BatchSpanProcessor"),
        ("LOGS", "OTLPLogExporter", "BatchLogRecordProcessor"),
    ],
)
def test_configure_closes_unowned_exporter(
    monkeypatch: pytest.MonkeyPatch,
    signal: str,
    exporter_name: str,
    owner_name: str,
) -> None:
    for name in ("METRICS", "TRACES", "LOGS"):
        monkeypatch.setenv(f"OTEL_{name}_EXPORTER", "none")
    monkeypatch.setenv(f"OTEL_{signal}_EXPORTER", "otlp")
    exporter = Mock()
    monkeypatch.setattr(otel, exporter_name, Mock(return_value=exporter))
    monkeypatch.setattr(
        otel,
        owner_name,
        Mock(side_effect=RuntimeError("owner creation failed")),
    )

    with pytest.raises(RuntimeError, match="owner creation failed"):
        otel.configure()

    exporter.shutdown.assert_called_once_with()


def test_configure_honors_disabled_signal_exporters() -> None:
    run_python("""
from os import environ
from unittest.mock import Mock

from dst_server.telemetry import otel

environ["OTEL_METRICS_EXPORTER"] = "none"
environ["OTEL_TRACES_EXPORTER"] = "NONE"
environ["OTEL_LOGS_EXPORTER"] = "otlp"
metric_exporter = Mock(side_effect=AssertionError("metrics exporter created"))
span_exporter = Mock(side_effect=AssertionError("trace exporter created"))
log_exporter = Mock(return_value=Mock())
otel.OTLPMetricExporter = metric_exporter
otel.OTLPSpanExporter = span_exporter
otel.OTLPLogExporter = log_exporter
otel.BatchLogRecordProcessor = Mock(return_value=Mock())

pipeline = otel.configure()
metric_exporter.assert_not_called()
span_exporter.assert_not_called()
log_exporter.assert_called_once()
pipeline.shutdown_sync()
""")


def test_configure_without_log_exporter_has_no_event_logger() -> None:
    run_python("""
from os import environ
from unittest.mock import Mock

from dst_server.telemetry import otel

environ["OTEL_METRICS_EXPORTER"] = "none"
environ["OTEL_TRACES_EXPORTER"] = "otlp"
environ["OTEL_LOGS_EXPORTER"] = "none"
otel.OTLPSpanExporter = lambda **_: Mock()
otel.BatchSpanProcessor = lambda *_, **__: Mock()

pipeline = otel.configure()
assert pipeline.logger is None
pipeline.shutdown_sync()
""")


def test_configure_installs_global_providers_once() -> None:
    run_python("""
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from time import sleep
from unittest.mock import Mock

from opentelemetry import metrics, trace
from opentelemetry._logs import get_logger_provider
from opentelemetry.sdk.metrics.export import InMemoryMetricReader
from ulid import ULID

from dst_server.telemetry import otel

otel.OTLPSpanExporter = lambda **_: Mock()
otel.OTLPLogExporter = lambda **_: Mock()
otel.BatchSpanProcessor = lambda *_, **__: Mock()
otel.BatchLogRecordProcessor = lambda *_, **__: Mock()
otel.PeriodicExportingMetricReader = lambda _: InMemoryMetricReader()

def metric_exporter():
    sleep(0.02)
    return Mock()

otel.OTLPMetricExporter = metric_exporter
barrier = Barrier(2)

def configure(_):
    barrier.wait()
    try:
        return otel.configure()
    except RuntimeError as error:
        return error

with ThreadPoolExecutor(2) as pool:
    results = list(pool.map(configure, range(2)))
pipelines = [result for result in results if isinstance(result, otel.Pipeline)]
errors = [result for result in results if isinstance(result, RuntimeError)]
assert len(pipelines) == len(errors) == 1
pipeline = pipelines[0]
assert str(errors[0]) == "OTLP providers have already been configured"
assert trace.get_tracer_provider() is pipeline.tracer_provider
assert metrics.get_meter_provider() is pipeline.meter_provider
assert get_logger_provider() is pipeline.logger_provider
instance_id = str(pipeline.tracer_provider.resource.attributes["service.instance.id"])
assert str(ULID.from_str(instance_id)) == instance_id
pipeline.shutdown_sync()
""")


def test_configure_failure_cleans_up_and_can_retry() -> None:
    run_python("""
from os import environ
from threading import enumerate as enumerate_threads

from opentelemetry.sdk._logs.export import InMemoryLogRecordExporter
from opentelemetry.sdk.metrics.export import ConsoleMetricExporter
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from dst_server.telemetry import otel

environ["OTEL_METRIC_EXPORT_INTERVAL"] = "60000"
otel.OTLPMetricExporter = ConsoleMetricExporter
otel.OTLPSpanExporter = lambda **_: InMemorySpanExporter()
otel.OTLPLogExporter = lambda **_: InMemoryLogRecordExporter()
real_batch_processor = otel.BatchSpanProcessor
failed = False

def fail_once(*args, **kwargs):
    global failed
    if not failed:
        failed = True
        raise RuntimeError("injected configuration failure")
    return real_batch_processor(*args, **kwargs)

otel.BatchSpanProcessor = fail_once

try:
    otel.configure()
except RuntimeError as error:
    assert str(error) == "injected configuration failure"
else:
    raise AssertionError("configuration unexpectedly succeeded")

def metric_threads():
    return [
        thread
        for thread in enumerate_threads()
        if thread.name == "OtelPeriodicExportingMetricReader" and thread.is_alive()
    ]

assert not metric_threads()
pipeline = otel.configure()
assert metric_threads()
pipeline.shutdown_sync()
assert not metric_threads()
""")


def test_pipeline_flush_and_shutdown_preserve_order_after_failure() -> None:
    calls: list[str] = []
    providers = {name: Mock() for name in ("logger", "tracer", "meter")}

    def call(operation: str, name: str) -> bool:
        calls.append(f"{operation}:{name}")
        if name == "logger":
            raise RuntimeError(operation)
        return True

    for name, provider in providers.items():
        provider.force_flush.side_effect = lambda _timeout, name=name: call(
            "flush", name
        )
        provider.shutdown.side_effect = lambda name=name: call("shutdown", name)
    pipeline = otel.Pipeline(
        logger=Mock(),
        logger_provider=providers["logger"],
        tracer_provider=providers["tracer"],
        meter_provider=providers["meter"],
    )

    with pytest.raises(RuntimeError, match="flush"):
        pipeline.force_flush_sync(100)
    assert calls == ["flush:logger", "flush:tracer", "flush:meter"]

    calls.clear()
    with pytest.raises(RuntimeError, match="shutdown"):
        pipeline.shutdown_sync()
    assert calls == ["shutdown:logger", "shutdown:tracer", "shutdown:meter"]


async def test_pipeline_shutdown_is_idempotent() -> None:
    logger_provider = Mock()
    tracer_provider = Mock()
    meter_provider = Mock()
    pipeline = otel.Pipeline(
        logger=Mock(),
        logger_provider=logger_provider,
        tracer_provider=tracer_provider,
        meter_provider=meter_provider,
    )

    await pipeline.shutdown()
    await pipeline.shutdown()

    logger_provider.shutdown.assert_called_once_with()
    tracer_provider.shutdown.assert_called_once_with()
    meter_provider.shutdown.assert_called_once_with()


async def test_otlp_pipeline_exports_traces_metrics_and_logs(  # ruff: ignore[too-many-locals]
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    span_exporter = InMemorySpanExporter()
    log_exporter = InMemoryLogRecordExporter()
    metric_reader = InMemoryMetricReader()
    instance_id = str(ULID())
    resource = Resource.create({
        "service.instance.id": instance_id,
        "service.namespace": "tests",
    })
    tracer_provider = TracerProvider(resource=resource, shutdown_on_exit=False)
    tracer_provider.add_span_processor(SimpleSpanProcessor(span_exporter))
    meter_provider = MeterProvider(
        resource=resource,
        metric_readers=(metric_reader,),
        shutdown_on_exit=False,
    )
    logger_provider = LoggerProvider(resource=resource, shutdown_on_exit=False)
    logger_provider.add_log_record_processor(SimpleLogRecordProcessor(log_exporter))
    pipeline = otel.Pipeline(
        logger=logger_provider.get_logger("dst-server.game-events"),
        tracer_provider=tracer_provider,
        meter_provider=meter_provider,
        logger_provider=logger_provider,
    )
    responses = [
        structured_result(True),
        'DST_SERVER_RESULT|{"ok":false,"error":"boom"}',
    ]

    execute = AsyncMock(side_effect=responses)

    monkeypatch.setattr(
        "dst_server.telemetry.recorder.trace.get_tracer",
        pipeline.tracer_provider.get_tracer,
    )
    monkeypatch.setattr(
        "dst_server.telemetry.recorder.metrics.get_meter",
        pipeline.meter_provider.get_meter,
    )
    recorder = Recorder("cluster", "test")
    events = stream.EventStream(recorder)
    game = GameClient(
        shard="test",
        lua_directory=Path(),
        telemetry=TelemetrySettings(),
        execute=execute,
        execute_ready=execute,
        execute_reload=AsyncMock(),
        wait_reload=AsyncMock(),
        recorder=recorder,
        session_id=lambda: "TEST",
        nonce=events.nonce,
    )

    try:
        parent_span_id = await exercise_pipeline(game, events, pipeline)

        finished_spans = span_exporter.get_finished_spans()
        assert len({span.name for span in finished_spans}) == len(finished_spans)
        spans = {span.name: span for span in finished_spans}
        span = spans["dst.server.lua.set_server_paused"]
        assert span.parent is not None
        assert span.parent.span_id == parent_span_id
        assert span.attributes is not None
        assert span.attributes["dst.lua.method"] == "set_server_paused"
        assert span.attributes["dst.session.id"] == "TEST"
        assert span.resource.attributes["service.instance.id"] == instance_id
        failed_span = spans["dst.server.lua.save"]
        assert failed_span.status.status_code is StatusCode.ERROR
        assert failed_span.attributes is not None
        assert failed_span.attributes["error.type"] == "builtins.RuntimeError"

        metric_data = metric_reader.get_metrics_data()
        assert metric_data is not None
        exported_metrics = {
            metric.name: metric
            for resource in metric_data.resource_metrics
            for scope in resource.scope_metrics
            for metric in scope.metrics
        }
        assert {
            "dst.player.action.count",
            "dst.server.operation.duration",
            "dst.server.player.count",
            "dst.telemetry.event.count",
        } <= exported_metrics.keys()
        action_points = exported_metrics["dst.player.action.count"].data.data_points
        assert any(
            point.attributes is not None
            and point.attributes["dst.action.name"] == "CHOP"
            and point.attributes["dst.action.success"] is True
            for point in action_points
        )
        operation_points = exported_metrics[
            "dst.server.operation.duration"
        ].data.data_points
        assert any(
            point.attributes is not None
            and point.attributes.get("error.type") == "builtins.RuntimeError"
            for point in operation_points
        )

        (readable,) = log_exporter.get_finished_logs()
        assert readable.log_record.event_name == "dst.player.action"
        assert readable.log_record.attributes is not None
        assert readable.log_record.attributes["dst.session.id"] == "TEST"
        assert readable.resource.attributes["service.namespace"] == "tests"
    finally:
        await pipeline.shutdown()
