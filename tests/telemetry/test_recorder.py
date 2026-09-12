import subprocess  # ruff: ignore[suspicious-subprocess-import]
import sys
from types import SimpleNamespace
from typing import cast
from unittest.mock import Mock

import orjson
import pytest
from logbook import TestHandler as CaptureHandler

from dst_server.telemetry import recorder as recorder_module
from dst_server.telemetry.otel import Pipeline
from dst_server.telemetry.recorder import Recorder


def test_recorder_preserves_trace_parent_errors_and_metric_outcomes() -> None:
    from opentelemetry.sdk.metrics import MeterProvider
    from opentelemetry.sdk.metrics.export import InMemoryMetricReader
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import SimpleSpanProcessor
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import (
        InMemorySpanExporter,
    )
    from opentelemetry.trace import StatusCode

    from dst_server.telemetry.recorder import Recorder

    exporter = InMemorySpanExporter()
    reader = InMemoryMetricReader()
    tracer_provider = TracerProvider(shutdown_on_exit=False)
    tracer_provider.add_span_processor(SimpleSpanProcessor(exporter))
    meter_provider = MeterProvider(metric_readers=(reader,), shutdown_on_exit=False)
    recorder = Recorder(
        "dst-000",
        "forest",
        tracer_provider=tracer_provider,
        meter_provider=meter_provider,
    )
    try:
        with tracer_provider.get_tracer("tests").start_as_current_span(
            "parent"
        ) as parent:
            with recorder.operation("save", "session"):
                recorder.record_event("accepted", event_name="dst.player.action")
                recorder.record_action("CHOP", True)
                recorder.set_process_up(True)
                recorder.set_player_count(1)
                recorder.set_client_count(2)
            message = "failed"
            with (
                pytest.raises(RuntimeError, match="failed"),
                recorder.operation("save", "session"),
            ):
                raise RuntimeError(message)
        spans = exporter.get_finished_spans()
        operations = [span for span in spans if span.name == "dst.server.save"]
        assert len(operations) == 2
        assert all(
            span.parent is not None
            and span.parent.span_id == parent.get_span_context().span_id
            for span in operations
        )
        assert operations[1].status.status_code is StatusCode.ERROR
        assert operations[1].attributes is not None
        assert operations[1].attributes["error.type"] == "builtins.RuntimeError"
        assert operations[0].attributes is not None
        assert operations[0].attributes["dst.session.id"] == "session"
        data = reader.get_metrics_data()
        assert data is not None
        names = {
            metric.name
            for resource in data.resource_metrics
            for scope in resource.scope_metrics
            for metric in scope.metrics
        }
        assert {
            "dst.player.action.count",
            "dst.server.operation.duration",
            "dst.server.player.count",
            "dst.server.client.count",
            "dst.telemetry.event.count",
        } <= names
    finally:
        tracer_provider.shutdown()
        meter_provider.shutdown()


def test_default_metrics_stay_bounded_and_bind_to_a_late_provider() -> None:
    source = """
import os
import weakref
from opentelemetry import metrics
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import InMemoryMetricReader
from dst_server.telemetry.recorder import Recorder

for name in tuple(os.environ):
    if name.startswith('OTEL_'):
        del os.environ[name]
early = Recorder('early', 'forest')
instruments = weakref.WeakSet()
for _ in range(100):
    recorder = Recorder('restarted', 'forest')
    instruments.add(recorder.operation_duration)
    del recorder
assert len(instruments) == 1, len(instruments)

reader = InMemoryMetricReader()
provider = MeterProvider(metric_readers=(reader,), shutdown_on_exit=False)
metrics.set_meter_provider(provider)
try:
    early.record_action('CHOP', True)
    Recorder('late', 'forest').record_action('CHOP', True)
    data = reader.get_metrics_data()
    points = [point for resource in data.resource_metrics
              for scope in resource.scope_metrics for metric in scope.metrics
              if metric.name == 'dst.player.action.count'
              for point in metric.data.data_points]
    assert {point.attributes['dst.cluster.name']: point.value for point in points} == {
        'early': 1, 'late': 1,
    }
finally:
    provider.shutdown()
"""
    subprocess.run(  # ruff: ignore[subprocess-without-shell-equals-true]
        [sys.executable, "-c", source], check=True, timeout=10
    )


def test_local_envelope_precedes_failed_submit_without_recursive_diagnostics() -> None:
    sink = SimpleNamespace(
        logs_enabled=True,
        resource=SimpleNamespace(
            attributes={"service.name": "test", "service.instance.id": "instance"}
        ),
        emit_operational=Mock(side_effect=OSError("private-exporter-detail")),
    )
    recorder = Recorder("cluster", "forest", pipeline=cast("Pipeline", sink))
    metrics = recorder.record_event = Mock()
    with CaptureHandler() as logs:
        for index in range(3):
            recorder.observe_log(
                event_name="dst.test",
                body={"message": "a\nb"},
                observed_timestamp_ns=123 + index,
                attributes={
                    "log.record.uid": f"uid-{index}",
                    "dst.game.attempt.id": "attempt",
                },
            )
    assert sink.emit_operational.call_count == 3
    records = [
        orjson.loads(record.message.split("DST_RECORD|", 1)[1])
        for record in logs.records
        if "DST_RECORD|" in record.message
    ]
    assert len(records) == 3
    assert records[0] == {
        "event_name": "dst.test",
        "body": {"message": "a\nb"},
        "observed_timestamp_ns": 123,
        "severity_text": "INFO",
        "attributes": {
            "log.record.uid": "uid-0",
            "dst.game.attempt.id": "attempt",
            "dst.cluster.name": "cluster",
            "dst.shard.name": "forest",
        },
        "resource": {"service.name": "test", "service.instance.id": "instance"},
    }
    assert all("\n" not in record.message for record in logs.records)
    assert all(
        "private-exporter-detail" not in record.message for record in logs.records
    )
    assert metrics.call_count == 3


@pytest.mark.parametrize("failure", ["handler", "serialization"])
def test_local_sink_failure_does_not_prevent_export_or_escape(
    monkeypatch: pytest.MonkeyPatch, failure: str
) -> None:
    sink = SimpleNamespace(
        logs_enabled=True,
        resource=SimpleNamespace(attributes={}),
        emit_operational=Mock(),
    )
    recorder = Recorder("cluster", "forest", pipeline=cast("Pipeline", sink))
    metrics = recorder.record_event = Mock()
    local = Mock()
    local.warning.side_effect = OSError("broken-warning-handler")
    if failure == "handler":
        local.info.side_effect = OSError("broken-info-handler")
    monkeypatch.setattr(recorder_module, "logger", local)
    recorder.observe_log(
        event_name="dst.test",
        body={"value": 2**64 if failure == "serialization" else 1},
        observed_timestamp_ns=1,
    )
    sink.emit_operational.assert_called_once()
    metrics.assert_called_once_with("dropped", reason="local_record_failed")
